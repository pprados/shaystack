"""
Add the persistance of time-series with TS database.

Set the HAYSTACK_TS with Timeseries database connection URL,
(timestream://HaystackAPIDemo/?mem_ttl=1&mag_ttl=100#haystack)
"""
import os
from datetime import datetime, date, time
from typing import Optional, Tuple, Callable, Any
from urllib.parse import parse_qs
from urllib.parse import urlparse

import boto3
import dateutil
import pytz
from botocore.client import BaseClient
from botocore.config import Config
from overrides import overrides

from .sql import Provider as SQLProvider
from .sql import log
from ..datatypes import Ref, MARKER, REMOVE, Coordinate, Quantity, NA, XStr
from ..grid import Grid

MAX_ROWS_BY_WRITE = 100
DEFAULT_MEM_TTL = 8766
DEFAULT_MAG_TTL = 400


def _create_database(client: BaseClient,
                     database: str) -> None:
    try:
        client.create_database(DatabaseName=database)
        log.info("Database [%s] created successfully.", database)
    except client.exceptions.ConflictException:
        # Database exists. Skipping database creation
        log.debug("Database [%s] exists. Skipping database creation.", database)


def _create_table(client: BaseClient,
                  database: str,
                  table_name: str,
                  mem_ttl: int,
                  mag_ttl: int) -> None:
    try:
        client.create_table(DatabaseName=database,
                            TableName=table_name,
                            RetentionProperties={
                                'MemoryStoreRetentionPeriodInHours': mem_ttl,
                                'MagneticStoreRetentionPeriodInDays': mag_ttl
                            })
        log.info("Table [%s] successfully created (memory ttl: %sh, magnetic ttl: %sd.",
                 table_name, mem_ttl, mag_ttl)
    except client.exceptions.ConflictException:
        # Table exists on database [{database}]. Skipping table creation"
        log.debug("Table [%s] exists. Skipping database creation.", table_name)


def _update_table(client: BaseClient,
                  database: str,
                  table_name: str,
                  mem_ttl: int,
                  mag_ttl: int) -> None:
    client.update_table(DatabaseName=database,
                        TableName=table_name,
                        RetentionProperties={
                            'MemoryStoreRetentionPeriodInHours': mem_ttl,
                            'MagneticStoreRetentionPeriodInDays': mag_ttl
                        })
    log.info("Retention updated to %sh and %sd.", mem_ttl, mag_ttl)


def _delete_table(client: BaseClient,
                  database: str,
                  table_name: str) -> None:
    try:
        client.delete_table(DatabaseName=database, TableName=table_name)
    except client.exceptions.ResourceNotFoundException:
        pass  # Ignore


class Provider(SQLProvider):
    """
    Expose an Haystack data via the Haystactk Rest API and SQL+TS databases
    """

    @property
    def name(self) -> str:
        return "SQL+timeseries"

    def __init__(self):
        super().__init__()
        log.info("Use %s", self._get_ts())
        self._parsed_ts = urlparse(self._get_ts())
        self._ts_table_name = self._parsed_ts.fragment
        if not self._ts_table_name:
            self._ts_table_name = "haystack"
        self._ts_database_name = self._parsed_ts.hostname
        self._boto = None
        self._write_client = None
        self._read_client = None

    def _get_boto(self):
        if not self._boto:
            self._boto = boto3.Session()
        return self._boto

    def _get_ts(self) -> str:  # pylint: disable=no-self-use
        """ Return the url to the file to expose. """
        return os.environ["HAYSTACK_TS"]

    def _get_write_client(self):
        if not self._write_client:
            region = os.environ.get("AWS_REGION",
                                    os.environ.get("AWS_DEFAULT_REGION"))
            self._write_client = self._get_boto().client('timestream-write',
                                                         region_name=region,
                                                         config=Config(read_timeout=10,
                                                                       max_pool_connections=5000,
                                                                       retries={'max_attempts': 3}
                                                                       )
                                                         )
        return self._write_client

    def _get_read_client(self):
        if not self._read_client:
            region = os.environ.get("AWS_REGION",
                                    os.environ.get("AWS_DEFAULT_REGION"))
            self._read_client = self._get_boto().client('timestream-query',
                                                        region_name=region)
        return self._read_client

    @overrides
    def import_ts_in_db(self, time_series: Grid,
                        entity_id: Ref,
                        customer_id: Optional[str],
                        now: Optional[datetime] = None
                        ) -> None:
        client = self._get_write_client()
        try:
            if not time_series:
                return  # Empty
            value = time_series[0]["val"]  # Suppose all values share the same type
            cast_fn, target_type = Provider._hs_to_timestream_type(value)
            if not customer_id:  # Empty string ?
                customer_id = None
            common_attributs = {
                'Dimensions': list(filter(lambda x: x['Value'] is not None, [
                    {'Name': 'id', 'Value': entity_id.name},
                    {'Name': 'hs_type', 'Value': type(value).__name__},
                    {'Name': 'unit', 'Value': value.unit if isinstance(value, Quantity) else " "},
                    {'Name': 'customer_id', 'Value': customer_id}
                ])),
                'MeasureName': 'val',
                'MeasureValueType': target_type,  # DOUBLE | BIGINT | VARCHAR | BOOLEAN
                'TimeUnit': 'MICROSECONDS',  # MILLISECONDS | SECONDS | MICROSECONDS | NANOSECONDS
                'Version': int(round(datetime.now().timestamp() * 1000000))
            }

            records = [{
                'Time': str(int(round(row["ts"].timestamp() * 1000000))),
                "MeasureValue": cast_fn(row["val"]),
            } for row in time_series]

            for i in range(0, len(records), MAX_ROWS_BY_WRITE):
                result = client.write_records(DatabaseName=self._ts_database_name,
                                              TableName=self._ts_table_name,
                                              Records=records[i:i + MAX_ROWS_BY_WRITE],
                                              CommonAttributes=common_attributs)
                log.debug("WriteRecords Status: [%s]", result['ResponseMetadata']['HTTPStatusCode'])
        except client.exceptions.RejectedRecordsException as err:
            log.error("RejectedRecords: %s", err)
            for rejected_record in err.response["RejectedRecords"]:
                log.error(' [%s:%s]: %s',
                          str(rejected_record["RecordIndex"]),
                          time_series[rejected_record["RecordIndex"]]["ts"],
                          rejected_record["Reason"]
                          )
            raise

    @staticmethod
    def _hs_to_timestream_type(value: Any) -> Tuple[Callable, str]:
        cast_fn = str
        if isinstance(value, str):
            target_type = "VARCHAR"
        elif isinstance(value, float):
            target_type = "DOUBLE"
        elif isinstance(value, Quantity):
            target_type = "DOUBLE"
            cast_fn = lambda x: str(x.value)
        elif isinstance(value, bool):
            target_type = "BOOLEAN"
        elif isinstance(value, int):
            target_type = "DOUBLE"
        elif value is MARKER:
            target_type = "BOOLEAN"
            cast_fn = lambda x: str(x is MARKER)
        elif value is REMOVE:
            target_type = "BOOLEAN"
            cast_fn = lambda x: str(x is REMOVE)
        elif value is NA:
            target_type = 'BOOLEAN'
            cast_fn = lambda x: str(x is NA)
        elif isinstance(value, Ref):
            target_type = "VARCHAR"
            cast_fn = lambda x: x.name
        elif isinstance(value, datetime):
            target_type = "BIGINT"
            cast_fn = lambda x: str(int(round(x.timestamp())))
        elif isinstance(value, date):
            target_type = "BIGINT"
            cast_fn = lambda x: str(x.toordinal())
        elif isinstance(value, time):
            target_type = "BIGINT"
            cast_fn = lambda x: str(((x.hour * 60 + x.minute) * 60 + x.second) * 1000000 + x.microsecond)
        elif isinstance(value, Coordinate):
            target_type = "VARCHAR"
            cast_fn = lambda x: str(x.latitude) + "," + str(x.longitude)
        elif isinstance(value, XStr):
            target_type = "VARCHAR"
            cast_fn = lambda x: value.encoding + "," + value.data_to_string()
        elif value is None:
            target_type = "BOOLEAN"
            cast_fn = lambda x: str(False)
        else:
            raise ValueError("Unknwon type")
        return cast_fn, target_type

    @staticmethod
    def _cast_timeserie_to_hs(val: str,
                              python_type: str,
                              unit: str) -> Any:
        if python_type == "str":
            return val
        if python_type == "float":
            return float(val)
        if python_type == "PintQuantity":
            return Quantity(float(val), unit)
        if python_type == "Quantity":
            return Quantity(float(val), unit)
        if python_type == "bool":
            return val.lower() == 'true'
        if python_type == "int":
            return int(float(val))
        if python_type == "MarkerType":
            return MARKER if val else None
        if python_type == "RemoveType":
            return REMOVE if val else None
        if python_type == "NAType":
            return NA if val else None
        if python_type == "Ref":
            return Ref(val)
        if python_type == "datetime":
            return datetime.fromtimestamp(int(val))
        if python_type == "date":
            return date.fromordinal(int(val))
        if python_type == "time":
            int_time = int(val)
            hour = ((int_time // 1000000) // 60) // 60
            minute = ((int_time // 1000000) // 60) % 60
            split = (int_time // 1000000) % 60
            mic = int_time % 1000000
            return time(hour, minute, split, mic)
        if python_type == "Coordinate":
            split = val.split(",")
            return Coordinate(float(split[0]), float(split[1]))
        if python_type == "XStr":
            split = val.split(",")
            return XStr(*split)
        if python_type == "NoneType":
            return None
        assert False, f"Unknown type {python_type}"
        return None

    _kind_type = {
        "marker": "BOOLEAN",
        "delete": "BOOLEAN",
        "bool": "BOOLEAN",
        "na": "BOOLEAN",
        "number": "DOUBLE",
        "remove": "BOOLEAN",
        "str": "VARCHAR",
        "uri": "VARCHAR",
        "ref": "VARCHAR",
        "date": "BIGINT",
        "time": "BIGINT",
        "datetime": "BIGINT",
        "coord": "VARCHAR",
        "xstr": "VARCHAR",
    }

    @staticmethod
    def _kind_to_timestream_type(kind: str) -> str:
        return Provider._kind_type[kind.lower()]

    @overrides
    def create_db(self) -> None:
        super().create_db()
        self.create_ts()

    @overrides
    def his_read(
            self,
            entity_id: Ref,
            dates_range: Optional[Tuple[datetime, datetime]] = None,
            date_version: Optional[datetime] = None,
    ) -> Grid:
        paginator = self._get_read_client().get_paginator('query')
        # To deduce the target type, read the haystack entity
        entity = self.read(1, None, [entity_id], None, date_version)[0]
        if not entity:
            raise ValueError(f" id '{entity_id} not found")

        if not date_version:
            date_version = datetime.max.replace(tzinfo=pytz.UTC)
        if dates_range and dates_range[1] > date_version:
            dates_range = list(dates_range)
            dates_range[1] = date_version

        kind = entity.get("kind", "Number")
        timestream_type = Provider._kind_to_timestream_type(kind)
        try:
            grid = Grid(columns=["ts", "val"])

            select_all = f"SELECT time,hs_type,unit,measure_value::{timestream_type} " \
                         f"FROM {self._ts_database_name}.{self._ts_table_name} " \
                         f"WHERE id='{entity_id.name}' AND customer_id='{self.get_customer_id()}' "
            if dates_range:
                select_all += f"AND time BETWEEN from_iso8601_timestamp('{dates_range[0].isoformat()}') " \
                              f"AND from_iso8601_timestamp('{dates_range[1].isoformat()}')"
            page_iterator = paginator.paginate(QueryString=select_all)
            for page in page_iterator:

                for row in page['Rows']:
                    datas = row['Data']
                    scalar_value = dateutil.parser.isoparse(datas[0]['ScalarValue'])
                    hs_type = datas[1]['ScalarValue']
                    unit = datas[2]['ScalarValue'].strip()
                    str_val = datas[3]['ScalarValue']
                    if not hs_type:
                        hs_type = "float"
                    grid.append({"ts": scalar_value,
                                 "val": Provider._cast_timeserie_to_hs(str_val, hs_type, unit)})
            return grid
        except ValueError as err:
            log.error("Exception while running query: %s", err)
            raise

    def create_ts(self) -> None:
        client = self._get_write_client()
        _create_database(client, self._ts_database_name)
        pqs = parse_qs(self._parsed_ts.query)
        mem_ttl = int(pqs["mem_ttl"][0]) if "mem_ttl" in pqs else DEFAULT_MEM_TTL
        mag_ttl = int(pqs["mag_ttl"][0]) if "mag_ttl" in pqs else DEFAULT_MAG_TTL
        _create_table(client, self._ts_database_name, self._ts_table_name, mem_ttl, mag_ttl)

    def purge_ts(self) -> None:
        _delete_table(self._get_write_client(), self._ts_database_name, self._ts_table_name)

    @overrides
    def purge_db(self) -> None:
        super().purge_db()
        self.purge_ts()