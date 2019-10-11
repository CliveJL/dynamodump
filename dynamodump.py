#!/usr/bin/env python3
"""
    Simple backup and restore script for Amazon DynamoDB using boto to work similarly to mysqldump.

    Suitable for DynamoDB usages of smaller data volume which do not warrant the usage of AWS
    Data Pipeline for backup/restores/empty.

    Example commands:

    Backup a Live DDB table to your local disk (default folder: dump/tablename)
    ./dynamodbdump.py -p default -r eu-west-1 --log INFO -m backup --skipThroughputUpdate -s iotl-network-device-registry

    Restore a Live DDB table from your local disk to another region with a new table name
    ./dynamodbdump.py -p default -r us-west-2 --log INFO -m restore --skipThroughputUpdate -s iotl-network-device-registry -d slj-live-copy-iotl-network-device-registry
"""

import argparse
import fnmatch
import json
import logging
import os
import shutil
import threading
import datetime
import errno
import sys
import time
import re
import zipfile
import tarfile
import decimal
import boto3

try:
    from queue import Queue
except ImportError:
    from Queue import Queue

try:
    from urllib.request import urlopen
    from urllib.error import URLError, HTTPError
except ImportError:
    from urllib2 import urlopen, URLError, HTTPError

JSON_INDENT = 2
AWS_SLEEP_INTERVAL = 10  # seconds
LOCAL_SLEEP_INTERVAL = 1  # seconds
BATCH_WRITE_SLEEP_INTERVAL = 0.15  # seconds
MAX_BATCH_WRITE = 25  # DynamoDB limit
SCHEMA_FILE = "schema.json"
DATA_DIR = "data"
MAX_RETRY = 6
LOCAL_REGION = "local"
LOG_LEVEL = "INFO"
DATA_DUMP = "dump"
RESTORE_WRITE_CAPACITY = 25
THREAD_START_DELAY = 1  # seconds
CURRENT_WORKING_DIR = os.getcwd()
DEFAULT_PREFIX_SEPARATOR = "-"
MAX_NUMBER_BACKUP_WORKERS = 25
METADATA_URL = "http://169.254.169.254/latest/meta-data/"


# Helper class to convert DynamoDB schema dump to valid JSON
class EncoderHelper(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, decimal.Decimal):
            if obj % 1 > 0:
                return float(obj)
            else:
                return int(obj)
        if isinstance(obj, set):
            return list(obj)
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        return json.JSONEncoder.default(self, obj)


def _get_aws_client(profile, region, service):
    """
    Build connection to some AWS service.
    """

    if region:
        aws_region = region
    else:
        aws_region = os.getenv("AWS_DEFAULT_REGION")

    # Fallback to querying metadata for region
    if not aws_region:
        try:
            azone = urlopen(METADATA_URL + "placement/availability-zone",
                            data=None, timeout=5).read().decode()
            aws_region = azone[:-1]
        except URLError:
            logging.exception("Timed out connecting to metadata service.\n\n")
            sys.exit(1)
        except HTTPError as e:
            logging.exception("Error determining region used for AWS client. Typo in code?\n\n" +
                              str(e))
            sys.exit(1)

    if profile:
        session = boto3.Session(profile_name=profile)
        client = session.client(service, region_name=aws_region)
    else:
        client = boto3.client(service, region_name=aws_region)
    return client


def get_table_name_by_tag(profile, region, tag):
    """
    Using provided connection to dynamodb and tag, get all tables that have provided tag

    Profile provided and, if needed, used to build connection to STS.
    """

    matching_tables = []
    all_tables = []
    sts = _get_aws_client(profile, region, "sts")
    dynamo = _get_aws_client(profile, region, "dynamodb")
    account_number = sts.get_caller_identity().get("Account")
    paginator = dynamo.get_paginator("list_tables")
    tag_key = tag.split("=")[0]
    tag_value = tag.split("=")[1]

    get_all_tables = paginator.paginate()
    for page in get_all_tables:
        for table in page["TableNames"]:
            all_tables.append(table)
            logging.debug("Found table " + table)

    for table in all_tables:
        table_arn = "arn:aws:dynamodb:{}:{}:table/{}".format(region, account_number, table)
        table_tags = dynamo.list_tags_of_resource(
            ResourceArn=table_arn
        )
        for found_tag in table_tags["Tags"]:
            if found_tag["Key"] == tag_key:
                logging.debug("Checking table " + table + " tag " + found_tag["Key"])
                if found_tag["Value"] == tag_value:
                    matching_tables.append(table)
                    logging.info("Matched table " + table)

    return matching_tables


def do_put_bucket_object(profile, region, bucket, bucket_object):
    """
    Put object into bucket.  Only called if we've also created an archive file with do_archive()

    Bucket must exist prior to running this function.
    profile could be None.
    bucket_object is file to be uploaded
    """

    s3 = _get_aws_client(profile, region, "s3")
    logging.info("Uploading backup to S3 bucket " + bucket)
    try:
        s3.upload_file(bucket_object, bucket, bucket_object,
                       ExtraArgs={
                           "ServerSideEncryption": "AES256"
                       })
    except ClientError as e:
        logging.exception("Failed to put file to S3 bucket\n\n" + str(e))
        sys.exit(1)


def do_get_s3_archive(profile, region, bucket, table, archive):
    """
    Fetch latest file named filename from S3

    Bucket must exist prior to running this function.
    filename is args.dumpPath.  File would be "args.dumpPath" with suffix .tar.bz2 or .zip
    """

    s3 = _get_aws_client(profile, region, "s3")

    if archive:
        if archive == "tar":
            archive_type = "tar.bz2"
        else:
            archive_type = "zip"

    # Make sure bucket exists before continuing
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        logging.exception("S3 bucket " + bucket + " does not exist. "
                          "Can't get backup file\n\n" + str(e))
        sys.exit(1)

    try:
        contents = s3.list_objects_v2(Bucket=bucket, Prefix=args.dumpPath)
    except ClientError as e:
        logging.exception("Issue listing contents of bucket " + bucket + "\n\n" + str(e))
        sys.exit(1)

    # Script will always overwrite older backup. Bucket versioning stores multiple backups.
    # Therefore, just get item from bucket based on table name since that's what we name the files.
    filename = None
    for d in contents["Contents"]:
        if d["Key"] == "{}/{}.{}".format(args.dumpPath, table, archive_type):
            filename = d["Key"]

    if not filename:
        logging.exception("Unable to find file to restore from.  "
                          "Confirm the name of the table you're restoring.")
        sys.exit(1)

    output_file = "/tmp/" + os.path.basename(filename)
    logging.info("Downloading file " + filename + " to " + output_file)
    s3.download_file(bucket, filename, output_file)

    # Extract archive based on suffix
    if tarfile.is_tarfile(output_file):
        try:
            logging.info("Extracting tar file...")
            with tarfile.open(name=output_file, mode="r:bz2") as a:
                a.extractall(path=".")
        except tarfile.ReadError as e:
            logging.exception("Error reading downloaded archive\n\n" + str(e))
            sys.exit(1)
        except tarfile.ExtractError as e:
            # ExtractError is raised for non-fatal errors on extract method
            logging.error("Error during extraction: " + str(e))

    # Assuming zip file here since we're only supporting tar and zip at this time
    else:
        try:
            logging.info("Extracting zip file...")
            with zipfile.ZipFile(output_file, "r") as z:
                z.extractall(path=".")
        except zipfile.BadZipFile as e:
            logging.exception("Problem extracting zip file\n\n" + str(e))
            sys.exit(1)


def do_archive(archive_type, dump_path):
    """
    Create compressed archive of dump_path.

    Accepts archive_type of zip or tar and requires dump_path, directory added to archive
    """

    archive_base = dump_path

    if archive_type.lower() == "tar":
        archive = archive_base + ".tar.bz2"
        try:
            logging.info("Creating tar file " + archive + "...")
            with tarfile.open(name=archive, mode="w:bz2") as a:
                for root, dirs, files in os.walk(archive_base):
                    for file in files:
                        a.add(os.path.join(root, file))
                return True, archive
        except tarfile.CompressionError as e:
            logging.exception("compression method is not supported or the data cannot be"
                              " decoded properly.\n\n" + str(e))
            sys.exit(1)
        except tarfile.TarError as e:
            logging.exception("Error creating tarfile archive.\n\n" + str(e))
            sys.exit(1)

    elif archive_type.lower() == "zip":
        try:
            logging.info("Creating zip file...")
            archive = archive_base + ".zip"
            with zipfile.ZipFile(archive, "w") as z:
                for root, dirs, files in os.walk(archive_base):
                    for file in files:
                        z.write(os.path.join(root, file))
                return True, archive
        except zipfile.BadZipFile as e:
            logging.exception("Problem creating zip file\n\n" + str(e))
            sys.exit(1)
        except zipfile.LargeZipFile:
            logging.exception("Zip file would be too large. Update code to use Zip64 to continue.")
            sys.exit(1)

    else:
        logging.error("Unsupported archive format received. Probably shouldn't have "
                      "made it to this code path. Skipping attempt at creating archive file")
        return False, None


def get_table_name_matches(conn, table_name_wildcard, separator):
    """
    Find tables to backup
    """

    all_tables = []
    last_evaluated_table_name = None

    # First iteration through list_tables
    table_list = conn.list_tables()
    all_tables.extend(table_list["TableNames"])

    # Further iterations based on LastEvaluatedTableName
    while 'LastEvaluatedTableName' in table_list:
        try:
            last_evaluated_table_name = table_list["LastEvaluatedTableName"]
            table_list = conn.list_tables(ExclusiveStartTableName=last_evaluated_table_name)
            all_tables.extend(table_list["TableNames"])
        except KeyError:
            break

    matching_tables = []
    for table_name in all_tables:
        if fnmatch.fnmatch(table_name, table_name_wildcard):
            logging.info("Adding %s", table_name)
            matching_tables.append(table_name)

    return matching_tables


def get_restore_table_matches(table_name_wildcard, separator):
    """
    Find tables to restore
    """

    matching_tables = []
    try:
        dir_list = os.listdir("./" + args.dumpPath)
    except OSError:
        logging.info("Cannot find \"./%s\", Now trying current working directory.."
                     % args.dumpPath)
        dump_data_path = CURRENT_WORKING_DIR
        try:
            dir_list = os.listdir(dump_data_path)
        except OSError:
            logging.info("Cannot find \"%s\" directory containing dump files!"
                         % dump_data_path)
            sys.exit(1)

    for dir_name in dir_list:
        if table_name_wildcard == "*":
            matching_tables.append(dir_name)
        elif separator == "":

            if dir_name.startswith(re.sub(r"([A-Z])", r" \1", table_name_wildcard.split("*", 1)[0])
                                   .split()[0]):
                matching_tables.append(dir_name)
        elif dir_name.split(separator, 1)[0] == table_name_wildcard.split("*", 1)[0]:
            matching_tables.append(dir_name)

    return matching_tables


def change_prefix(source_table_name, source_wildcard, destination_wildcard, separator):
    """
    Update prefix used for searching tables
    """

    source_prefix = source_wildcard.split("*", 1)[0]
    destination_prefix = destination_wildcard.split("*", 1)[0]
    if separator == "":
        if re.sub(r"([A-Z])", r" \1", source_table_name).split()[0] == source_prefix:
            return destination_prefix + re.sub(r"([A-Z])", r" \1", source_table_name)\
                .split(" ", 1)[1].replace(" ", "")
    if source_table_name.split(separator, 1)[0] == source_prefix:
        return destination_prefix + separator + source_table_name.split(separator, 1)[1]


def mkdir_p(path):
    """
    Create directory to hold dump
    """

    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


def batch_write(conn, sleep_interval, table_name, put_requests):
    """
    Write data to table_name
    """

    request_items = {table_name: put_requests}
    i = 1
    sleep = sleep_interval
    while True:
        response = conn.batch_write_item(RequestItems=request_items)
        unprocessed_items = response["UnprocessedItems"]

        if len(unprocessed_items) == 0:
            break
        if len(unprocessed_items) > 0 and i <= MAX_RETRY:
            logging.debug(str(len(unprocessed_items)) +
                          " unprocessed items, retrying after %s seconds.. [%s/%s]"
                          % (str(sleep), str(i), str(MAX_RETRY)))
            request_items = unprocessed_items
            time.sleep(sleep)
            sleep += sleep_interval
            i += 1
        else:
            logging.info("Max retries reached, failed to processed batch write: " +
                         json.dumps(unprocessed_items, indent=JSON_INDENT))
            logging.info("Ignoring and continuing..")
            break


def wait_for_active_table(conn, table_name, verb):
    """
    Wait for table to be indesired state
    """

    while True:
        if conn.describe_table(TableName=table_name)["Table"]["TableStatus"] != "ACTIVE":
            logging.info("Waiting for " + table_name + " table to be " + verb + ".. [" +
                         conn.describe_table(TableName=table_name)["Table"]["TableStatus"] + "]")
            time.sleep(sleep_interval)
        else:
            logging.info(table_name + " " + verb + ".")
            break


def update_provisioned_throughput(conn, table_name, read_capacity, write_capacity, wait=True):
    """
    Update provisioned throughput on the table to provided values
    """

    logging.info("Updating " + table_name + " table read capacity to: " +
                 str(read_capacity) + ", write capacity to: " + str(write_capacity))
    while True:
        try:
            conn.update_table(TableName=table_name, ProvisionedThroughput={"ReadCapacityUnits": int(read_capacity),
                               "WriteCapacityUnits": int(write_capacity)})
            break
        except boto.exception.JSONResponseError as e:
            if e.body["__type"] == "com.amazonaws.dynamodb.v20120810#LimitExceededException":
                logging.info("Limit exceeded, retrying updating throughput of " + table_name + "..")
                time.sleep(sleep_interval)
            elif e.body["__type"] == "com.amazon.coral.availability#ThrottlingException":
                logging.info("Control plane limit exceeded, retrying updating throughput"
                             "of " + table_name + "..")
                time.sleep(sleep_interval)

    # wait for provisioned throughput update completion
    if wait:
        wait_for_active_table(conn, table_name, "updated")


def do_backup(dynamo, read_capacity, tableQueue=None, srcTable=None):
    """
    Connect to DynamoDB and perform the backup for srcTable or each table in tableQueue
    """

    if srcTable:
        table_name = srcTable

    if tableQueue:
        while True:
            table_name = tableQueue.get()
            if table_name is None:
                break

            logging.info("Starting backup for " + table_name + "..")

            # Trash data, re-create subdir
            if os.path.exists(args.dumpPath + os.sep + table_name):
                shutil.rmtree(args.dumpPath + os.sep + table_name)
            mkdir_p(args.dumpPath + os.sep + table_name)

            # Get table schema
            logging.info("Dumping table schema for " + table_name)
            f = open(args.dumpPath + os.sep + table_name + os.sep + SCHEMA_FILE, "w+")
            table_desc = dynamo.describe_table(TableName=table_name)
            f.write(json.dumps(table_desc, indent=JSON_INDENT, cls=EncoderHelper))
            f.close()

            billingMode = table_desc.get('Table').get('BillingModeSummary', {}).get('BillingMode', {})

            if not args.schemaOnly:
                original_read_capacity = \
                    table_desc["Table"]["ProvisionedThroughput"]["ReadCapacityUnits"]
                original_write_capacity = \
                    table_desc["Table"]["ProvisionedThroughput"]["WriteCapacityUnits"]

                # Override table read capacity if specified
                if billingMode != "PAY_PER_REQUEST":
                    if read_capacity is not None and read_capacity != original_read_capacity:
                        update_provisioned_throughput(dynamo, table_name,
                                                      read_capacity, original_write_capacity)

                # Get table data
                logging.info("Dumping table items for " + table_name)
                mkdir_p(args.dumpPath + os.sep + table_name + os.sep + DATA_DIR)

                # First scan operation, first write to dump file
                i = 1
                try:
                    scanned_table = dynamo.scan(TableName=table_name)
                except ProvisionedThroughputExceededException:
                    logging.error("EXCEEDED THROUGHPUT ON TABLE " +
                                  table_name + ".  BACKUP FOR IT IS USELESS.")
                    tableQueue.task_done()

                f = open(
                    args.dumpPath + os.sep + table_name + os.sep + DATA_DIR + os.sep +
                    str(i).zfill(4) + ".json", "w+"
                )
                f.write(json.dumps(scanned_table, indent=JSON_INDENT))
                f.close()

                # Page through scans based on LastEvaluatedKey, incrementing dump filename
                i += 1
                while 'LastEvaluatedKey' in scanned_table:
                    try:
                        scanned_table = dynamo.scan(TableName=table_name, ExclusiveStartKey=scanned_table['LastEvaluatedKey'])
                    except ProvisionedThroughputExceededException:
                        logging.error("EXCEEDED THROUGHPUT ON TABLE " +
                                      table_name + ".  BACKUP FOR IT IS USELESS.")
                        tableQueue.task_done()

                    f = open(
                        args.dumpPath + os.sep + table_name + os.sep + DATA_DIR + os.sep +
                        str(i).zfill(4) + ".json", "w+"
                    )
                    f.write(json.dumps(scanned_table, indent=JSON_INDENT))
                    f.close()

                    i += 1

                # Revert back to original table read capacity if specified
                if billingMode != "PAY_PER_REQUEST":
                    if read_capacity is not None and read_capacity != original_read_capacity:
                        update_provisioned_throughput(dynamo,
                                                      table_name,
                                                      original_read_capacity,
                                                      original_write_capacity,
                                                      False)

                logging.info("Backup for " + table_name + " table completed. Time taken: " + str(
                    datetime.datetime.now().replace(microsecond=0) - start_time))

            tableQueue.task_done()


def do_restore(dynamo, sleep_interval, source_table, destination_table, write_capacity):
    """
    Restore table
    """
    logging.info("Starting restore for " + source_table + " to " + destination_table + "..")

    # create table using schema
    # restore source_table from dump directory if it exists else try current working directory
    if os.path.exists("%s/%s" % (args.dumpPath, source_table)):
        dump_data_path = args.dumpPath
    else:
        logging.info("Cannot find \"./%s/%s\", Now trying current working directory.."
                     % (args.dumpPath, source_table))
        if os.path.exists("%s/%s" % (CURRENT_WORKING_DIR, source_table)):
            dump_data_path = CURRENT_WORKING_DIR
        else:
            logging.info("Cannot find \"%s/%s\" directory containing dump files!"
                         % (CURRENT_WORKING_DIR, source_table))
            sys.exit(1)
    table_data = json.load(open(dump_data_path + os.sep + source_table + os.sep + SCHEMA_FILE))
    table = table_data["Table"]
    table_attribute_definitions = table["AttributeDefinitions"]
    table_table_name = destination_table
    table_key_schema = table["KeySchema"]
    original_read_capacity = table["ProvisionedThroughput"]["ReadCapacityUnits"]
    original_write_capacity = table["ProvisionedThroughput"]["WriteCapacityUnits"]
    table_local_secondary_indexes = table.get("LocalSecondaryIndexes")
    table_global_secondary_indexes = table.get("GlobalSecondaryIndexes")
    try:
        table_billing_mode = table['BillingModeSummary']['BillingMode']
    except KeyError:
        table_billing_mode = 'PROVISIONED'

    # override table write capacity if specified, else use RESTORE_WRITE_CAPACITY if original
    # write capacity is lower
    if write_capacity is None:
        if original_write_capacity < RESTORE_WRITE_CAPACITY:
            write_capacity = RESTORE_WRITE_CAPACITY
        else:
            write_capacity = original_write_capacity

    # override GSI write capacities if specified, else use RESTORE_WRITE_CAPACITY if original
    # write capacity is lower
    original_gsi_write_capacities = []
    if table_global_secondary_indexes is not None:
        for gsi in table_global_secondary_indexes:
            original_gsi_write_capacities.append(gsi["ProvisionedThroughput"]["WriteCapacityUnits"])

            if gsi["ProvisionedThroughput"]["WriteCapacityUnits"] < int(write_capacity):
                gsi["ProvisionedThroughput"]["WriteCapacityUnits"] = int(write_capacity)
            # Delete unnecessary keys from schema dump ready for create_table
            [gsi.pop(key, None) for key in ['IndexStatus','IndexSizeBytes','ItemCount','IndexArn']]
            [gsi["ProvisionedThroughput"].pop(key, None) for key in ['LastDecreaseDateTime','NumberOfDecreasesToday','LastIncreaseDateTime']]
            if table_billing_mode == 'PAY_PER_REQUEST':
                [gsi.pop(key, None) for key in ['ProvisionedThroughput']]

    # Prepare LSI schema for create_table
    if table_local_secondary_indexes is not None:
        for lsi in table_local_secondary_indexes:
            # Delete unnecessary keys from schema dump ready for create_table
            [lsi.pop(key, None) for key in ['IndexSizeBytes','ItemCount','IndexArn']]

    # temp provisioned throughput for restore
    table_provisioned_throughput = {"ReadCapacityUnits": int(original_read_capacity),
                                    "WriteCapacityUnits": int(write_capacity)}

    if not args.dataOnly:
        while True:
            try:
                if table_billing_mode == 'PAY_PER_REQUEST':
                    logging.info("Creating " + destination_table + " table with Billing Mode PAY_PER_REQUEST" )
                    if table_global_secondary_indexes is not None:
                        dynamo.create_table(AttributeDefinitions=table_attribute_definitions, TableName=table_table_name, KeySchema=table_key_schema,
                                        BillingMode=table_billing_mode, GlobalSecondaryIndexes=table_global_secondary_indexes)
                        break
                    elif table_local_secondary_indexes is not None:
                        dynamo.create_table(AttributeDefinitions=table_attribute_definitions, TableName=table_table_name, KeySchema=table_key_schema,
                                        BillingMode=table_billing_mode, LocalSecondaryIndexes=table_local_secondary_indexes)
                        break
                    else:
                        dynamo.create_table(AttributeDefinitions=table_attribute_definitions, TableName=table_table_name, KeySchema=table_key_schema,
                                        BillingMode=table_billing_mode)
                        break
                else:
                    logging.info("Creating " + destination_table + " table with temp write capacity of " + str(write_capacity))
                    if table_global_secondary_indexes is not None:
                        dynamo.create_table(AttributeDefinitions=table_attribute_definitions, TableName=table_table_name, KeySchema=table_key_schema,
                                    BillingMode=table_billing_mode, ProvisionedThroughput=table_provisioned_throughput,
                                    GlobalSecondaryIndexes=table_global_secondary_indexes)
                        break
                    elif table_local_secondary_indexes is not None:
                        dynamo.create_table(AttributeDefinitions=table_attribute_definitions, TableName=table_table_name, KeySchema=table_key_schema,
                                    BillingMode=table_billing_mode, ProvisionedThroughput=table_provisioned_throughput,
                                    LocalSecondaryIndexes=table_local_secondary_indexes)
                        break
                    else:
                        dynamo.create_table(AttributeDefinitions=table_attribute_definitions, TableName=table_table_name, KeySchema=table_key_schema,
                                    BillingMode=table_billing_mode, ProvisionedThroughput=table_provisioned_throughput)
                    break

            except boto.exception.JSONResponseError as e:
                if e.body["__type"] == "com.amazonaws.dynamodb.v20120810#LimitExceededException":
                    logging.info("Limit exceeded, retrying creation of " + destination_table + "..")
                    time.sleep(sleep_interval)
                elif e.body["__type"] == "com.amazon.coral.availability#ThrottlingException":
                    logging.info("Control plane limit exceeded, "
                                 "retrying creation of " + destination_table + "..")
                    time.sleep(sleep_interval)
                else:
                    logging.exception(e)
                    sys.exit(1)

        # wait for table creation completion
        wait_for_active_table(dynamo, destination_table, "created")
    else:
        # update provisioned capacity
        if int(write_capacity) > original_write_capacity:
            update_provisioned_throughput(dynamo,
                                          destination_table,
                                          original_read_capacity,
                                          write_capacity,
                                          False)

    if not args.schemaOnly:
        # Read data files
        logging.info("Restoring data for " + destination_table + " table..")
        data_file_list = os.listdir(dump_data_path + os.sep + source_table +
                                    os.sep + DATA_DIR + os.sep)
        data_file_list.sort()

        for data_file in data_file_list:
            logging.info("Processing " + data_file + " of " + destination_table)
            items = []
            item_data = json.load(
                open(
                    dump_data_path + os.sep + source_table + os.sep + DATA_DIR + os.sep + data_file
                )
            )
            items.extend(item_data["Items"])

            # batch write data
            put_requests = []
            while len(items) > 0:
                put_requests.append({"PutRequest": {"Item": items.pop(0)}})

                # flush every MAX_BATCH_WRITE
                if len(put_requests) == MAX_BATCH_WRITE:
                    logging.debug("Writing next " + str(MAX_BATCH_WRITE) +
                                  " items to " + destination_table + "..")
                    batch_write(dynamo, BATCH_WRITE_SLEEP_INTERVAL, destination_table, put_requests)
                    del put_requests[:]

            # flush remainder
            if len(put_requests) > 0:
                batch_write(dynamo, BATCH_WRITE_SLEEP_INTERVAL, destination_table, put_requests)

        if not args.skipThroughputUpdate:
            # revert to original table write capacity if it has been modified
            if int(write_capacity) != original_write_capacity:
                update_provisioned_throughput(dynamo,
                                              destination_table,
                                              original_read_capacity,
                                              original_write_capacity,
                                              False)

            # loop through each GSI to check if it has changed and update if necessary
            if table_global_secondary_indexes is not None:
                gsi_data = []
                for gsi in table_global_secondary_indexes:
                    wcu = gsi["ProvisionedThroughput"]["WriteCapacityUnits"]
                    rcu = gsi["ProvisionedThroughput"]["ReadCapacityUnits"]
                    original_gsi_write_capacity = original_gsi_write_capacities.pop(0)
                    if original_gsi_write_capacity != wcu:
                        gsi_data.append({
                            "Update": {
                                "IndexName": gsi["IndexName"],
                                "ProvisionedThroughput": {
                                    "ReadCapacityUnits":
                                        int(rcu),
                                    "WriteCapacityUnits": int(original_gsi_write_capacity)
                                }
                            }
                        })

                logging.info("Updating " + destination_table +
                             " global secondary indexes write capacities as necessary..")
                while True:
                    try:
                        dynamo.update_table(destination_table,
                                            global_secondary_index_updates=gsi_data)
                        break
                    except boto.exception.JSONResponseError as e:
                        if (e.body["__type"] ==
                                "com.amazonaws.dynamodb.v20120810#LimitExceededException"):
                            logging.info(
                                "Limit exceeded, retrying updating throughput of"
                                "GlobalSecondaryIndexes in " + destination_table + "..")
                            time.sleep(sleep_interval)
                        elif (e.body["__type"] ==
                              "com.amazon.coral.availability#ThrottlingException"):
                            logging.info(
                                "Control plane limit exceeded, retrying updating throughput of"
                                "GlobalSecondaryIndexes in " + destination_table + "..")
                            time.sleep(sleep_interval)

        # Wait for table to become active
        wait_for_active_table(dynamo, destination_table, "active")

        logging.info("Restore for " + source_table + " to " + destination_table +
                     " table completed. Time taken: " + str(
                         datetime.datetime.now().replace(microsecond=0) - start_time))
    else:
        logging.info("Empty schema of " + source_table + " table created. Time taken: " +
                     str(datetime.datetime.now().replace(microsecond=0) - start_time))


def main():
    """
    Entrypoint to the script
    """

    global args, sleep_interval, start_time

    # parse args
    parser = argparse.ArgumentParser(description="Simple DynamoDB backup/restore")
    group = parser.add_mutually_exclusive_group()
    parser.add_argument("-a", "--archive", help="Type of compressed archive to create."
                        "If unset, don't create archive. Required for S3 bucket.", choices=["zip", "tar"])
    parser.add_argument("-b", "--bucket", help="S3 bucket in which to store or retrieve backups."
                        "[must already exist and --archive must be specified]")
    parser.add_argument("-m", "--mode", help="Operation to perform",
                        choices=["backup", "restore"], required=True)
    parser.add_argument("-r", "--region", help="AWS region to use, e.g. 'eu-west-1'.", required=True)
    parser.add_argument("-p", "--profile",
                        help="AWS credentials file profile to use.")
    group.add_argument("-s", "--srcTable",
                        help="Source DynamoDB table name to backup or restore from, "
                        "use 'tablename*' for wildcard prefix selection or '*' for "
                        "all tables.  Mutually exclusive with --tag")
    parser.add_argument("-d", "--destTable",
                        help="Destination DynamoDB table name to backup or restore to, "
                        "use 'tablename*' for wildcard prefix selection "
                        "(defaults to use '-' separator) [optional, defaults to source]")
    parser.add_argument("--prefixSeparator", help="Specify a different prefix separator, "
                        "e.g. '.' [optional]")
    parser.add_argument("--noSeparator", action='store_true',
                        help="Overrides the use of a prefix separator for backup wildcard "
                        "searches [optional]")
    parser.add_argument("--readCapacity",
                        help="Change the temp read capacity of the DynamoDB table to backup "
                        "from [optional]")
    group.add_argument("-t", "--tag", help="Tag to use for identifying tables to back up.  "
                        "Mutually exclusive with --srcTable.  Provided as KEY=VALUE")
    parser.add_argument("--writeCapacity",
                        help="Change the temp write capacity of the DynamoDB table to restore "
                        "to [defaults to " + str(RESTORE_WRITE_CAPACITY) + ", optional]")
    parser.add_argument("--schemaOnly", action="store_true", default=False,
                        help="Backup or restore the schema only. Do not backup/restore data. "
                        "Can be used with both backup and restore modes. Cannot be used with "
                        "the --dataOnly [optional]")
    parser.add_argument("--dataOnly", action="store_true", default=False,
                        help="Restore data only. Do not delete/recreate schema [optional for "
                        "restore]")
    parser.add_argument("--skipThroughputUpdate", action="store_true", default=False,
                        help="Skip updating throughput values across tables [optional]")
    parser.add_argument("--dumpPath", help="Directory to place and search for DynamoDB table "
                        "backups (defaults to use '" + str(DATA_DUMP) + "') [optional]",
                        default=str(DATA_DUMP))
    parser.add_argument("--log", help="Logging level - DEBUG|INFO|WARNING|ERROR|CRITICAL "
                        "[optional]")
    args = parser.parse_args()

    # Set log level
    log_level = LOG_LEVEL
    if args.log is not None:
        log_level = args.log.upper()
    logging.basicConfig(level=getattr(logging, log_level))

    # Check to make sure that --dataOnly and --schemaOnly weren't simultaneously specified
    if args.schemaOnly and args.dataOnly:
        logging.warning("Options --schemaOnly and --dataOnly are mutually exclusive.")
        sys.exit(1)

    # Check that an archive type is specified for backup to S3
    if args.bucket and not args.archive:
        logging.warning("An archive type (-a) must be specified for S3 bucket use")
        sys.exit(1)

    # Instantiate connection
    if not args.profile:
        conn = boto3.client('dynamodb', region_name=args.region)
        sleep_interval = AWS_SLEEP_INTERVAL
    else:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        conn = session.client('dynamodb')
        sleep_interval = AWS_SLEEP_INTERVAL

    # Don't proceed if connection is not established
    if not conn:
        logging.info("Unable to establish connection with DynamoDB")
        sys.exit(1)

    # Set prefix separator
    prefix_separator = DEFAULT_PREFIX_SEPARATOR
    if args.prefixSeparator is not None:
        prefix_separator = args.prefixSeparator
    if args.noSeparator is True:
        prefix_separator = None

    # Do backup/restore
    start_time = datetime.datetime.now().replace(microsecond=0)
    if args.mode == "backup":
        matching_backup_tables = []
        if args.tag:
            # Use Boto3 to find tags.
            matching_backup_tables = get_table_name_by_tag(args.profile, args.region, args.tag)
        elif args.srcTable.find("*") != -1:
            matching_backup_tables = get_table_name_matches(conn, args.srcTable, prefix_separator)
        elif args.srcTable:
            matching_backup_tables.append(args.srcTable)

        if len(matching_backup_tables) == 0:
            logging.info("No matching tables found. Nothing to do.")
            sys.exit(0)
        else:
            logging.info("Found " + str(len(matching_backup_tables)) +
                         " table(s) in DynamoDB in " + args.region + " to backup: " +
                         ", ".join(matching_backup_tables))

        try:
            if args.srcTable.find("*") == -1:
                do_backup(conn, args.read_capacity, tableQueue=None)
            else:
                do_backup(conn, args.read_capacity, matching_backup_tables)
        except AttributeError:
            # Didn't specify srcTable if we get here

            q = Queue()
            threads = []

            for i in range(MAX_NUMBER_BACKUP_WORKERS):
                t = threading.Thread(target=do_backup, args=(conn, args.readCapacity),
                                     kwargs={"tableQueue": q})
                t.start()
                threads.append(t)
                time.sleep(THREAD_START_DELAY)

            for table in matching_backup_tables:
                q.put(table)

            q.join()

            for i in range(MAX_NUMBER_BACKUP_WORKERS):
                q.put(None)
            for t in threads:
                t.join()

            try:
                logging.info("Backup of table(s) " + args.srcTable + " completed!")
            except (NameError, TypeError):
                logging.info("Backup of table(s) " +
                             ", ".join(matching_backup_tables) + " completed!")

            if args.archive:
                if args.tag:
                    for table in matching_backup_tables:
                        dump_path = args.dumpPath + os.sep + table
                        did_archive, archive_file = do_archive(args.archive, dump_path)
                        if args.bucket and did_archive:
                            do_put_bucket_object(args.profile,
                                                 args.region,
                                                 args.bucket,
                                                 archive_file)
                else:
                    did_archive, archive_file = do_archive(args.archive, args.dumpPath)

                if args.bucket and did_archive:
                    do_put_bucket_object(args.profile, args.region, args.bucket, archive_file)

    elif args.mode == "restore":
        if args.destTable is not None:
            dest_table = args.destTable
        else:
            dest_table = args.srcTable

        # If backups are in S3, download and extract the backup to use during restoration
        if args.bucket:
            do_get_s3_archive(args.profile, args.region, args.bucket, args.srcTable, args.archive)

        if dest_table.find("*") != -1:
            matching_destination_tables = get_table_name_matches(conn, dest_table, prefix_separator)
            delete_str = ": " if args.dataOnly else " to be deleted: "
            if len(matching_destination_tables) > 0:
                logging.warning(
                    "Found " + str(len(matching_destination_tables)) +
                    " table(s) already in DynamoDB in " + args.region + ": " +
                    ", ".join(matching_destination_tables))
                sys.exit(1)
            else:
                logging.info(
                    "Found " + str(len(matching_destination_tables)) +
                    " table(s) in DynamoDB in " + args.region + delete_str +
                    ", ".join(matching_destination_tables))


            threads = []
            for table in matching_destination_tables:
                t = threading.Thread(target=delete_table, args=(conn, sleep_interval, table))
                threads.append(t)
                t.start()
                time.sleep(THREAD_START_DELAY)

            for thread in threads:
                thread.join()

            matching_restore_tables = get_restore_table_matches(args.srcTable, prefix_separator)
            logging.info(
                "Found " + str(len(matching_restore_tables)) +
                " table(s) in " + args.dumpPath + " to restore: " + ", ".join(
                    matching_restore_tables))

            threads = []
            for source_table in matching_restore_tables:
                if args.srcTable == "*":
                    t = threading.Thread(target=do_restore,
                                         args=(conn,
                                               sleep_interval,
                                               source_table,
                                               source_table,
                                               args.writeCapacity))
                else:
                    t = threading.Thread(target=do_restore,
                                         args=(conn, sleep_interval, source_table,
                                               change_prefix(source_table,
                                                             args.srcTable,
                                                             dest_table,
                                                             prefix_separator),
                                               args.writeCapacity))
                threads.append(t)
                t.start()
                time.sleep(THREAD_START_DELAY)

            for thread in threads:
                thread.join()

            logging.info("Restore of table(s) " + args.srcTable + " to " +
                         dest_table + " completed!")
        else:
            do_restore(conn, sleep_interval, args.srcTable, dest_table, args.writeCapacity)


if __name__ == "__main__":
    main()
