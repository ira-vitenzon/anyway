# -*- coding: utf-8 -*-
import logging
import datetime
import json
import enum
from functools import lru_cache
from typing import Optional, Dict, List, Any, Callable
from dataclasses import dataclass, field

import pandas as pd
from collections import defaultdict
from sqlalchemy import func
from sqlalchemy import cast, Numeric
from sqlalchemy import desc
from anyway.backend_constants import BE_CONST
from anyway.models import NewsFlash, AccidentMarkerView, InvolvedMarkerView, RoadSegments
from anyway.parsers import resolution_dict
from anyway.app_and_db import db
from anyway.infographics_dictionaries import driver_type_hebrew_dict
from anyway.infographics_dictionaries import head_on_collisions_comparison_dict
from anyway.parsers import infographics_data_cache_updater

"""
    Base class for widgets. Each widget will be a class that is derived from Widget, and instantiated with RequestParams instance. It will need to implement the methods in the base class
    The Serialize() method returns the data that the API returns, and has the following structure:
    {
        'name': str,
        'data': {
                 'items': list (Array) | dictionary (Object),
                 'text': dictionary (Object) - can be empty
                 },
        'meta': {
                 'rank': int (Integer)
                 }
    }
"""


@enum.unique
class WidgetId(enum.Enum):
    accident_count_by_severity = [1, 'accident_count_by_severity']
    most_severe_accidents_table = [2, 'most_severe_accidents_table']
    most_severe_accidents = [3, 'most_severe_accidents']
    street_view = [4, 'street_view']
    head_on_collisions_comparison = [5, "head_on_collisions_comparison"]
    accident_count_by_accident_type = [6, "accident_count_by_accident_type"]
    accidents_heat_map = [7, "accidents_heat_map"]
    accident_count_by_accident_year = [8, "accident_count_by_accident_year"]
    injured_count_by_accident_year = [9, "injured_count_by_accident_year"]
    accident_count_by_day_night = [10, "accident_count_by_day_night"]
    accidents_count_by_hour = [11, "accidents_count_by_hour"]
    accident_count_by_road_light = [12, "accident_count_by_road_light"]
    top_road_segments_accidents_per_km = [13, "top_road_segments_accidents_per_km"]
    injured_count_per_age_group = [14, "injured_count_per_age_group"]
    vision_zero = [15, "vision_zero"]
    accident_count_by_driver_type = [16, "accident_count_by_driver_type"]
    accident_count_by_car_type = [17, "accident_count_by_car_type"]
    injured_accidents_with_pedestrians = [18, "injured_accidents_with_pedestrians"]
    accident_severity_by_cross_location = [19, "accident_severity_by_cross_location"]
    motorcycle_accidents_vs_all_accidents = \
        [20, "motorcycle_accidents_vs_all_accidents"]
    accidents_count_pedestrians_per_vehicle_street_vs_all = \
        [21, "accidents_count_pedestrians_per_vehicle_street_vs_all"]

    def get_rank(self) -> int:
        return self.value[0]

    def get_name(self) -> str:
        return self.value[1]


class RequestParams:
    """
    Input for infographics data generation, per api call
    """
    def __init__(self,
                 news_flash_id: int,
                 location_text: str,
                 location_info: Optional[dict],
                 resolution: Dict,
                 gps: Dict,
                 start_time: datetime.date,
                 end_time: datetime.date):
        self.news_flash_is = news_flash_id
        self.location_text = location_text
        self.location_info = location_info
        self.resolution = resolution,
        self.gps = gps
        self.start_time = start_time
        self.end_time = end_time

    def __str__(self):
        return f'RequestParams(location_text:{self.location_text}, start_time:{self.start_time}, end_time:{self.end_time} '


@dataclass
class Widget:
    request_params: RequestParams
    name: str = field(init=False)
    rank: int = field(init=False, default=-1)
    items: Dict = field(init=False)
    text: str = field(init=False, default=None)
    meta: Optional[Dict] = field(init=False, default=None)

    def get_name(self) -> str:
        pass

    def get_rank(self) -> int:
        pass

    def get_widget_id(self) -> WidgetId:
        pass

    def serialize(self):
        output = {}
        output["name"] = self.name
        output["data"] = {}
        output["data"]["items"] = self.items
        if self.text:
            output["data"]["text"] = self.text
        if self.meta:
            output["meta"] = self.meta
        else:
            output["meta"] = {}
        output["meta"]["rank"] = self.rank
        return output


class WidgetDef:
    """
    Static info per widget that is needed to generate the widget data
    """
    def __init__(self,
                 widget_id,
                 generate_items,
                 is_included_in_response=None,
                 is_included_in_cache=True):
        self.widget_id: WidgetId = widget_id
        self.generate_items: Callable[[RequestParams, WidgetDef], Widget] = generate_items
        self.is_included_in_response: Optional[Callable[[Widget], bool]] = is_included_in_response
        self.is_included_in_cache: bool = is_included_in_cache

    def get_name(self) -> str:
        return self.widget_id.get_name()

    def get_rank(self) -> int:
        return self.widget_id.get_rank()

    def __str__(self):
        return f"WidgetDef(name:{self.get_name()}, rank:{self.get_rank()}, items_func:{self.generate_items})"


def extract_news_flash_location(news_flash_id):
    news_flash_obj = db.session.query(NewsFlash).filter(NewsFlash.id == news_flash_id).first()
    if not news_flash_obj:
        logging.warning(f"could not find news flash id {str(news_flash_id)}")
        return None
    resolution = news_flash_obj.resolution if news_flash_obj.resolution else None
    if not news_flash_obj or not resolution or resolution not in resolution_dict:
        logging.warning(f"could not find valid resolution for news flash id {str(news_flash_id)}")
        return None
    data = {"resolution": resolution}
    for field in resolution_dict[resolution]:
        curr_field = getattr(news_flash_obj, field)
        if curr_field is not None:
            data[field] = curr_field
    gps = {}
    for field in ["lon", "lat"]:
        gps[field] = getattr(news_flash_obj, field)
    return {"name": "location", "data": data, "gps": gps}


def get_query(table_obj, filters, start_time, end_time):
    query = db.session.query(table_obj)
    if start_time:
        query = query.filter(getattr(table_obj, "accident_timestamp") >= start_time)
    if end_time:
        query = query.filter(getattr(table_obj, "accident_timestamp") <= end_time)
    if filters:
        for field_name, value in filters.items():
            if isinstance(value, list):
                values = value
            else:
                values = [value]
            query = query.filter((getattr(table_obj, field_name)).in_(values))
    return query


def get_accident_count_by_accident_type(location_info, start_time, end_time):
    all_accident_type_count = get_accidents_stats(
        table_obj=AccidentMarkerView,
        filters=location_info,
        group_by="accident_type_hebrew",
        count="accident_type_hebrew",
        start_time=start_time,
        end_time=end_time,
    )
    merged_accident_type_count = [{"accident_type": "התנגשות", "count": 0}]
    for item in all_accident_type_count:
        if "התנגשות" in item["accident_type"]:
            merged_accident_type_count[0]["count"] += item["count"]
        else:
            merged_accident_type_count.append(item)
    return merged_accident_type_count


def get_top_road_segments_accidents_per_km(
        resolution, location_info, start_time=None, end_time=None, limit=5
):
    if resolution != "כביש בינעירוני":  # relevent for non urban roads only
        return {}

    query = get_query(
        table_obj=AccidentMarkerView, filters=None, start_time=start_time, end_time=end_time
    )

    query = (
        query.with_entities(
            AccidentMarkerView.road_segment_name,
            func.count(AccidentMarkerView.road_segment_name).label("total_accidents"),
            (RoadSegments.to_km - RoadSegments.from_km).label("segment_length"),
            cast(
                (
                        func.count(AccidentMarkerView.road_segment_name)
                        / (RoadSegments.to_km - RoadSegments.from_km)
                ),
                Numeric(10, 4),
            ).label("accidents_per_km"),
        )
            .filter(AccidentMarkerView.road1 == RoadSegments.road)
            .filter(AccidentMarkerView.road_segment_number == RoadSegments.segment)
            .filter(AccidentMarkerView.road1 == location_info["road1"])
            .filter(AccidentMarkerView.road_segment_name is not None)
            .group_by(AccidentMarkerView.road_segment_name, RoadSegments.from_km, RoadSegments.to_km)
            .order_by(desc("accidents_per_km"))
            .limit(limit)
    )

    result = pd.read_sql_query(query.statement, query.session.bind)
    return result.to_dict(orient="records")  # pylint: disable=no-member


def get_accidents_stats(
        table_obj, filters=None, group_by=None, count=None, start_time=None, end_time=None
):
    filters = filters or {}
    filters["provider_code"] = [
        BE_CONST.CBS_ACCIDENT_TYPE_1_CODE,
        BE_CONST.CBS_ACCIDENT_TYPE_3_CODE,
    ]
    # get stats
    query = get_query(table_obj, filters, start_time, end_time)
    if group_by:
        query = query.group_by(group_by)
        query = query.with_entities(group_by, func.count(count))
    df = pd.read_sql_query(query.statement, query.session.bind)
    df.rename(columns={"count_1": "count"}, inplace=True)  # pylint: disable=no-member
    df.columns = [c.replace("_hebrew", "") for c in df.columns]
    return (  # pylint: disable=no-member
        df.to_dict(orient="records") if group_by or count else df.to_dict()
    )


def get_injured_filters(location_info):
    new_filters = {}
    for curr_filter, curr_values in location_info.items():
        if curr_filter in ["region_hebrew", "district_hebrew", "district_hebrew", "yishuv_name"]:
            new_filter_name = "accident_" + curr_filter
            new_filters[new_filter_name] = curr_values
        else:
            new_filters[curr_filter] = curr_values
    new_filters["injury_severity"] = [1, 2, 3, 4, 5]
    return new_filters


def get_most_severe_accidents_with_entities(
        table_obj, filters, entities, start_time, end_time, limit=10
):
    filters = filters or {}
    filters["provider_code"] = [
        BE_CONST.CBS_ACCIDENT_TYPE_1_CODE,
        BE_CONST.CBS_ACCIDENT_TYPE_3_CODE,
    ]
    query = get_query(table_obj, filters, start_time, end_time)
    query = query.with_entities(*entities)
    query = query.order_by(
        getattr(table_obj, "accident_severity"), getattr(table_obj, "accident_timestamp").desc()
    )
    query = query.limit(limit)
    df = pd.read_sql_query(query.statement, query.session.bind)
    df.columns = [c.replace("_hebrew", "") for c in df.columns]
    return df.to_dict(orient="records")  # pylint: disable=no-member


def get_most_severe_accidents(table_obj, filters, start_time, end_time, limit=10):
    entities = (
        "longitude",
        "latitude",
        "accident_severity_hebrew",
        "accident_timestamp",
        "accident_type_hebrew",
    )
    return get_most_severe_accidents_with_entities(
        table_obj, filters, entities, start_time, end_time, limit
    )


def get_accidents_heat_map(table_obj, filters, start_time, end_time):
    filters = filters or {}
    filters["provider_code"] = [
        BE_CONST.CBS_ACCIDENT_TYPE_1_CODE,
        BE_CONST.CBS_ACCIDENT_TYPE_3_CODE,
    ]
    query = get_query(table_obj, filters, start_time, end_time)
    query = query.with_entities("longitude", "latitude")
    df = pd.read_sql_query(query.statement, query.session.bind)
    return df.to_dict(orient="records")  # pylint: disable=no-member


def filter_and_group_injured_count_per_age_group(data_of_ages):
    import re

    range_dict = {0: 14, 15: 24, 25: 64, 65: 200}
    dict_by_required_age_group = defaultdict(int)

    for age_range_and_count in data_of_ages:
        age_range = age_range_and_count["age_group"]
        count = age_range_and_count["count"]

        # Parse the db age range
        match_parsing = re.match("([0-9]{2})\\-([0-9]{2})", age_range)
        if match_parsing:
            regex_age_matches = match_parsing.groups()
            if len(regex_age_matches) != 2:
                dict_by_required_age_group["unknown"] += count
                continue
            min_age_raw, max_age_raw = regex_age_matches
        else:
            match_parsing = re.match("([0-9]{2})\\+", age_range)  # e.g  85+
            if match_parsing:
                # We assume that no body live beyond age 200
                min_age_raw, max_age_raw = match_parsing.group(1), 200
            else:
                dict_by_required_age_group["unknown"] += count
                continue

        # Find to what "bucket" to aggregate the data
        min_age = int(min_age_raw)
        max_age = int(max_age_raw)
        for item in range_dict.items():
            item_min_range, item_max_range = item
            if (
                    item_min_range <= min_age <= item_max_range
                    and item_min_range <= max_age <= item_max_range
            ):
                string_age_range = f"{item_min_range:02}-{item_max_range:02}"
                dict_by_required_age_group[string_age_range] += count
                break

    # Rename the last key
    dict_by_required_age_group["65+"] = dict_by_required_age_group["65-200"]
    del dict_by_required_age_group["65-200"]

    # Modify return value to wanted format
    items = []
    for key in dict_by_required_age_group:
        items.append({"age_group": key, "count": dict_by_required_age_group[key]})

    return items


def get_most_severe_accidents_table_title(location_text):
    return "תאונות חמורות ב" + location_text


def get_accident_count_by_severity(data: RequestParams, widget_def: WidgetDef) -> Widget:
    count_by_severity = get_accidents_stats(
        table_obj=AccidentMarkerView,
        filters=data.location_info,
        group_by="accident_severity_hebrew",
        count="accident_severity_hebrew",
        start_time=data.start_time,
        end_time=data.end_time,
    )
    severity_dict = {"קטלנית": "fatal", "קשה": "severe", "קלה": "light"}
    items = {}
    total_accidents_count = 0
    start_year = data.start_time.year
    end_year = data.end_time.year
    for severity_and_count in count_by_severity:
        accident_severity_hebrew = severity_and_count["accident_severity"]
        severity_english = severity_dict[accident_severity_hebrew]
        severity_count_text = "severity_{}_count".format(severity_english)
        items[severity_count_text] = severity_and_count["count"]
        total_accidents_count += severity_and_count["count"]
    items["start_year"] = start_year
    items["end_year"] = end_year
    items["total_accidents_count"] = total_accidents_count
    widget = Widget(
        widget_id=widget_def.widget_id,
        items=items)
    return widget


def get_most_severe_accidents_table(request_params: RequestParams, widget_def: WidgetDef):
    entities = "id", "provider_code", "accident_timestamp", "accident_type_hebrew", "accident_year"
    accidents = get_most_severe_accidents_with_entities(
        table_obj=AccidentMarkerView,
        filters=request_params.location_info,
        entities=entities,
        start_time=request_params.start_time,
        end_time=request_params.end_time,
    )
    # Add casualties
    for accident in accidents:
        accident["type"] = accident["accident_type"]
        dt = accident["accident_timestamp"].to_pydatetime()
        accident["date"] = dt.strftime("%d/%m/%y")
        accident["hour"] = dt.strftime("%H:%M")
        accident["killed_count"] = get_casualties_count_in_accident(
            accident["id"], accident["provider_code"], 1, accident["accident_year"]
        )
        accident["severe_injured_count"] = get_casualties_count_in_accident(
            accident["id"], accident["provider_code"], 2, accident["accident_year"]
        )
        accident["light_injured_count"] = get_casualties_count_in_accident(
            accident["id"], accident["provider_code"], 3, accident["accident_year"]
        )
        # TODO: remove injured_count after FE adaptation to light and severe counts
        accident["injured_count"] = (
                accident["severe_injured_count"] + accident["light_injured_count"]
        )
        del (
            accident["accident_timestamp"],
            accident["accident_type"],
            accident["id"],
            accident["provider_code"],
        )

    res = Widget(
        WidgetId.most_severe_accidents_table,
        items=accidents,
        text={"title": get_most_severe_accidents_table_title(request_params.location_text)},
    )

    return res


def count_accidents_by_driver_type(data):
    driver_types = defaultdict(int)
    for item in data:
        vehicle_type, count = item["involve_vehicle_type"], int(item["count"])
        if vehicle_type in BE_CONST.PROFESSIONAL_DRIVER_VEHICLE_TYPES:
            driver_types[driver_type_hebrew_dict["professional_driver"]] += count
        elif vehicle_type in BE_CONST.PRIVATE_DRIVER_VEHICLE_TYPES:
            driver_types[driver_type_hebrew_dict["private_vehicle_driver"]] += count
        elif (
                vehicle_type in BE_CONST.LIGHT_ELECTRIC_VEHICLE_TYPES
                or vehicle_type in BE_CONST.OTHER_VEHICLES_TYPES
        ):
            driver_types[driver_type_hebrew_dict["other_driver"]] += count
    output = []
    for driver_type, count in driver_types.items():
        output.append({"driver_type": driver_type, "count": count})
    return output


# count of dead and severely injured
def get_casualties_count_in_accident(accident_id, provider_code, injury_severity, accident_year):
    filters = {
        "accident_id": accident_id,
        "provider_code": provider_code,
        "injury_severity": injury_severity,
        "accident_year": accident_year,
    }
    casualties = get_accidents_stats(
        table_obj=InvolvedMarkerView,
        filters=filters,
        group_by="injury_severity",
        count="injury_severity",
    )
    res = 0
    for ca in casualties:
        res += ca["count"]
    return res


# generate text describing location or road segment of news flash
# to be used by most severe accidents additional info widget
def get_news_flash_location_text(news_flash_id):
    news_flash_item = db.session.query(NewsFlash).filter(NewsFlash.id == news_flash_id).first()
    nf = news_flash_item.serialize()
    resolution = nf["resolution"] if nf["resolution"] else ""
    yishuv_name = nf["yishuv_name"] if nf["yishuv_name"] else ""
    road1 = str(int(nf["road1"])) if nf["road1"] else ""
    road2 = str(int(nf["road2"])) if nf["road2"] else ""
    street1_hebrew = nf["street1_hebrew"] if nf["street1_hebrew"] else ""
    road_segment_name = nf["road_segment_name"] if nf["road_segment_name"] else ""
    if resolution == "כביש בינעירוני" and road1 and road_segment_name:
        res = "כביש " + road1 + " במקטע " + road_segment_name
    elif resolution == "עיר" and not yishuv_name:
        res = nf["location"]
    elif resolution == "עיר" and yishuv_name:
        res = nf["yishuv_name"]
    elif resolution == "צומת בינעירוני" and road1 and road2:
        res = "צומת כביש " + road1 + " עם כביש " + road2
    elif resolution == "צומת בינעירוני" and road1 and road_segment_name:
        res = "כביש " + road1 + " במקטע " + road_segment_name
    elif resolution == "רחוב" and yishuv_name and street1_hebrew:
        res = " רחוב " + street1_hebrew + " ב" + yishuv_name
    else:
        logging.warning(
            "Did not found quality resolution. Using location field. News Flash id:{}".format(
                nf["id"]
            )
        )
        res = nf["location"]
    return res


def extract_news_flash_obj(news_flash_id):
    news_flash_obj = db.session.query(NewsFlash).filter(NewsFlash.id == news_flash_id).first()

    if not news_flash_obj:
        logging.warning("Could not find news flash id {}".format(news_flash_id))
        return None

    return news_flash_obj


def sum_road_accidents_by_specific_type(road_data, field_name):
    dict_merge = defaultdict(int)
    dict_merge[field_name] = 0
    dict_merge[head_on_collisions_comparison_dict["others"]] = 0

    for accident_data in road_data:
        if accident_data["accident_type"] == field_name:
            dict_merge[field_name] += accident_data["count"]
        else:
            dict_merge[head_on_collisions_comparison_dict["others"]] += accident_data["count"]
    return dict_merge


def convert_roads_fatal_accidents_to_frontend_view(data_dict):
    data_list = []
    for key, value in data_dict.items():
        if key == head_on_collisions_comparison_dict["head_to_head_collision"]:
            data_list.append({"desc": head_on_collisions_comparison_dict["head_to_head"], "count": value})
        else:
            data_list.append({"desc": key, "count": value})

    return data_list


def get_head_to_head_stat(news_flash_id, start_time, end_time):
    news_flash_obj = extract_news_flash_obj(news_flash_id)
    road_data = {}
    filter_dict = {
        "road_type": BE_CONST.ROAD_TYPE_NOT_IN_CITY_NOT_IN_INTERSECTION,
        "accident_severity": BE_CONST.ACCIDENT_SEVERITY_DEADLY,
    }
    all_roads_data = get_accidents_stats(
        table_obj=AccidentMarkerView,
        filters=filter_dict,
        group_by="accident_type_hebrew",
        count="accident_type_hebrew",
        start_time=start_time,
        end_time=end_time,
    )

    if news_flash_obj.road1 and news_flash_obj.road_segment_name:
        filter_dict.update(
            {"road1": news_flash_obj.road1, "road_segment_name": news_flash_obj.road_segment_name}
        )
        road_data = get_accidents_stats(
            table_obj=AccidentMarkerView,
            filters=filter_dict,
            group_by="accident_type_hebrew",
            count="accident_type_hebrew",
            start_time=start_time,
            end_time=end_time,
        )

    road_data_dict = sum_road_accidents_by_specific_type(road_data, "התנגשות חזית בחזית")
    all_roads_data_dict = sum_road_accidents_by_specific_type(all_roads_data, "התנגשות חזית בחזית")

    return {
        "specific_road_segment_fatal_accidents": convert_roads_fatal_accidents_to_frontend_view(
            road_data_dict
        ),
        "all_roads_fatal_accidents": convert_roads_fatal_accidents_to_frontend_view(
            all_roads_data_dict
        ),
    }


# gets the latest date an accident has occured
def get_latest_accident_date(table_obj, filters):
    filters = filters or {}
    filters["provider_code"] = [
        BE_CONST.CBS_ACCIDENT_TYPE_1_CODE,
        BE_CONST.CBS_ACCIDENT_TYPE_3_CODE,
    ]
    query = db.session.query(func.max(table_obj.accident_timestamp))
    df = pd.read_sql_query(query.statement, query.session.bind)
    return (df.to_dict(orient="records"))[0].get("max_1")  # pylint: disable=no-member


def percentage_accidents_by_car_type(involved_by_vehicle_type_data):
    driver_types = defaultdict(float)
    total_count = 0
    for item in involved_by_vehicle_type_data:
        vehicle_type, count = item["involve_vehicle_type"], int(item["count"])
        total_count += count
        if vehicle_type in BE_CONST.CAR_VEHICLE_TYPES:
            driver_types["רכב פרטי"] += count
        elif vehicle_type in BE_CONST.LARGE_VEHICLE_TYPES:
            driver_types["מסחרי/משאית"] += count
        elif vehicle_type in BE_CONST.MOTORCYCLE_VEHICLE_TYPES:
            driver_types["אופנוע"] += count
        elif vehicle_type in BE_CONST.BICYCLE_AND_SMALL_MOTOR_VEHICLE_TYPES:
            driver_types["אופניים/קורקינט"] += count
        else:
            driver_types["אחר"] += count

    output = defaultdict(float)
    for k, v in driver_types.items():  # Calculate percentage
        output[k] = 100 * v / total_count

    return output


@lru_cache(maxsize=64)
def percentage_accidents_by_car_type_national_data_cache(start_time, end_time):
    involved_by_vehicle_type_data = get_accidents_stats(
        table_obj=InvolvedMarkerView,
        group_by="involve_vehicle_type",
        count="involve_vehicle_type",
        start_time=start_time,
        end_time=end_time,
    )
    return percentage_accidents_by_car_type(involved_by_vehicle_type_data)


def stats_accidents_by_car_type_with_national_data(
        involved_by_vehicle_type_data, start_time, end_time
):
    out = []
    data_by_segment = percentage_accidents_by_car_type(involved_by_vehicle_type_data)
    national_data = percentage_accidents_by_car_type_national_data_cache(start_time, end_time)

    for k, v in national_data.items():  # pylint: disable=W0612
        out.append(
            {
                "car_type": k,
                "percentage_segment": data_by_segment[k],
                "percentage_country": national_data[k],
            }
        )

    return out


def get_widgets_def() -> List[WidgetDef]:
    return [
        WidgetDef(WidgetId.accident_count_by_severity, get_accident_count_by_severity),
        WidgetDef(WidgetId.most_severe_accidents_table, get_most_severe_accidents_table)
        # {'name': 'severity', 'rank': 2, 'generate_items': accident_count},
        # {'name': 'color', 'rank': 3, 'generate_items': accident_count, 'text': "text"}
    ]


def generate_widgets(request_params: RequestParams, widgets_def: List[WidgetDef], to_cache: bool=True) -> List[Dict]:
    logging.debug(f"generate_widgets: selection params:{request_params}")
    res = []
    for item in list(filter(lambda wd: wd.is_included_in_cache == to_cache, widgets_def)):
        logging.debug(f"generate_widgets: processing widget:{item.get_name()}")
        item_widget = item.generate_items(request_params, item)
        if item.is_included_in_response and item.is_included_in_response(item_widget):
            res.append(item_widget.serialize())
    return res


def get_request_params(news_flash_id: int, number_of_years_ago: int) -> Optional[RequestParams]:
    try:
        number_of_years_ago = int(number_of_years_ago)
    except ValueError:
        return None
    if number_of_years_ago < 0 or number_of_years_ago > 100:
        return None
    location_info = extract_news_flash_location(news_flash_id)
    if location_info is None:
        return None
    logging.debug("location_info:{}".format(location_info))
    location_text = get_news_flash_location_text(news_flash_id)
    logging.debug("location_text:{}".format(location_text))
    gps = location_info["gps"]
    location_info = location_info["data"]
    resolution = location_info.pop("resolution")
    if resolution is None:
        return None

    if all(value is None for value in location_info.values()):
        return None

    last_accident_date = get_latest_accident_date(table_obj=AccidentMarkerView, filters=None)
    # converting to datetime object to get the date
    end_time = last_accident_date.to_pydatetime().date()

    start_time = datetime.date(end_time.year + 1 - number_of_years_ago, 1, 1)

    request_params = RequestParams(
        news_flash_id=news_flash_id,
        location_text=location_text,
        location_info=location_info,
        resolution=resolution,
        gps=gps,
        start_time=start_time,
        end_time=end_time)
    return request_params


def create_infographics_data(news_flash_id, number_of_years_ago):
    request_params = get_request_params(news_flash_id, number_of_years_ago)
    location_info = request_params.location_info
    resolution = request_params.resolution
    start_time = request_params.start_time
    end_time = request_params.end_time
    location_text = request_params.location_text

    if request_params is None:
        return {}

    output = {}
    output["meta"] = {"location_info": location_info.copy(), "location_text": location_text}
    output["widgets"] = []
    output["widgets"].append(generate_widgets(request_params, get_widgets_def()))
    # accident_severity count
    # items = get_accident_count_by_severity(
    #     location_info=location_info,
    #     location_text=location_text,
    #     start_time=start_time,
    #     end_time=end_time,
    # )
    #
    # accident_count_by_severity = Widget(name="accident_count_by_severity", rank=1, items=items)
    # output["widgets"].append(accident_count_by_severity.serialize())

    # most severe accidents table
    # most_severe_accidents_table = Widget(
    #     WidgetId.most_severe_accidents_table,
    #     items=get_most_severe_accidents_table(location_info, start_time, end_time),
    #     text={"title": get_most_severe_accidents_table_title(location_text)},
    # )
    # output["widgets"].append(most_severe_accidents_table.serialize())

    # most severe accidents
    most_severe_accidents = Widget(
        WidgetId.most_severe_accidents,
        items=get_most_severe_accidents(
            table_obj=AccidentMarkerView,
            filters=location_info,
            start_time=start_time,
            end_time=end_time,
        ),
    )
    output["widgets"].append(most_severe_accidents.serialize())

    # street view
    street_view = Widget(
        WidgetId.street_view, items={"longitude": request_params.gps["lon"],
                                     "latitude": request_params.gps["lat"]}
    )
    output["widgets"].append(street_view.serialize())

    # head to head accidents
    head_on_collisions_comparison = Widget(
        WidgetId.head_on_collisions_comparison,
        items=get_head_to_head_stat(
            news_flash_id=news_flash_id, start_time=start_time, end_time=end_time
        ),
    )
    output["widgets"].append(head_on_collisions_comparison.serialize())

    # accident_type count
    accident_count_by_accident_type = Widget(
        WidgetId.accident_count_by_accident_type,
        items=get_accident_count_by_accident_type(
            location_info=location_info, start_time=start_time, end_time=end_time
        ),
    )
    output["widgets"].append(accident_count_by_accident_type.serialize())

    # accidents heat map
    accidents_heat_map = Widget(
        WidgetId.accidents_heat_map,
        items=get_accidents_heat_map(
            table_obj=AccidentMarkerView,
            filters=location_info,
            start_time=start_time,
            end_time=end_time,
        ),
    )
    output["widgets"].append(accidents_heat_map.serialize())

    # accident count by accident year
    accident_count_by_accident_year = Widget(
        WidgetId.accident_count_by_accident_year,
        items=get_accidents_stats(
            table_obj=AccidentMarkerView,
            filters=location_info,
            group_by="accident_year",
            count="accident_year",
            start_time=start_time,
            end_time=end_time,
        ),
        text={"title": "כמות תאונות"},
    )
    output["widgets"].append(accident_count_by_accident_year.serialize())

    # injured count by accident year
    injured_count_by_accident_year = Widget(
        WidgetId.injured_count_by_accident_year,
        items=get_accidents_stats(
            table_obj=InvolvedMarkerView,
            filters=get_injured_filters(location_info),
            group_by="accident_year",
            count="accident_year",
            start_time=start_time,
            end_time=end_time,
        ),
        text={"title": "כמות פצועים"},
    )
    output["widgets"].append(injured_count_by_accident_year.serialize())

    # accident count on day light
    accident_count_by_day_night = Widget(
        WidgetId.accident_count_by_day_night,
        items=get_accidents_stats(
            table_obj=AccidentMarkerView,
            filters=location_info,
            group_by="day_night_hebrew",
            count="day_night_hebrew",
            start_time=start_time,
            end_time=end_time,
        ),
        text={"title": "כמות תאונות ביום ובלילה"},
    )
    output["widgets"].append(accident_count_by_day_night.serialize())

    # accidents distribution count by hour
    accidents_count_by_hour = Widget(
        WidgetId.accidents_count_by_hour,
        items=get_accidents_stats(
            table_obj=AccidentMarkerView,
            filters=location_info,
            group_by="accident_hour",
            count="accident_hour",
            start_time=start_time,
            end_time=end_time,
        ),
        text={"title": "כמות תאונות לפי שעה"},
    )
    output["widgets"].append(accidents_count_by_hour.serialize())

    # accident count by road_light
    accident_count_by_road_light = Widget(
        WidgetId.accident_count_by_road_light,
        items=get_accidents_stats(
            table_obj=AccidentMarkerView,
            filters=location_info,
            group_by="road_light_hebrew",
            count="road_light_hebrew",
            start_time=start_time,
            end_time=end_time,
        ),
        text={"title": "כמות תאונות לפי תאורה"},
    )
    output["widgets"].append(accident_count_by_road_light.serialize())

    # accident count by road_segment
    top_road_segments_accidents_per_km = Widget(
        WidgetId.top_road_segments_accidents_per_km,
        items=get_top_road_segments_accidents_per_km(
            resolution=resolution,
            location_info=location_info,
            start_time=start_time,
            end_time=end_time,
        ),
    )
    output["widgets"].append(top_road_segments_accidents_per_km.serialize())

    # injured count per age group
    data_of_injured_count_per_age_group_raw = get_accidents_stats(
        table_obj=InvolvedMarkerView,
        filters=get_injured_filters(location_info),
        group_by="age_group_hebrew",
        count="age_group_hebrew",
        start_time=start_time,
        end_time=end_time,
    )
    data_of_injured_count_per_age_group = filter_and_group_injured_count_per_age_group(
        data_of_injured_count_per_age_group_raw
    )
    injured_count_per_age_group = Widget(
        WidgetId.injured_count_per_age_group, items=data_of_injured_count_per_age_group
    )
    output["widgets"].append(injured_count_per_age_group.serialize())

    # vision zero
    vision_zero = Widget(WidgetId.vision_zero, items=["vision_zero_2_plus_1"])
    output["widgets"].append(vision_zero.serialize())

    # involved by driver type
    involved_by_vehicle_type_data = get_accidents_stats(
        table_obj=InvolvedMarkerView,
        filters=get_injured_filters(location_info),
        group_by="involve_vehicle_type",
        count="involve_vehicle_type",
        start_time=start_time,
        end_time=end_time,
    )
    accident_count_by_driver_type = Widget(
        WidgetId.accident_count_by_driver_type,
        items=count_accidents_by_driver_type(involved_by_vehicle_type_data),
    )
    output["widgets"].append(accident_count_by_driver_type.serialize())

    # involved by car type
    accident_count_by_car_type = Widget(
        WidgetId.accident_count_by_car_type,
        items=stats_accidents_by_car_type_with_national_data(
            involved_by_vehicle_type_data, start_time, end_time
        ),
    )
    output["widgets"].append(accident_count_by_car_type.serialize())

    def injured_accidents_with_pedestrians_mock_data():  # Temporary for Frontend
        return [{'year': 2009,
                 'light_injury_severity_text': "פצוע קל",
                 'light_injury_severity_count': 12,
                 'severe_injury_severity_text': "פצוע קשה",
                 'severe_injury_severity_count': 3,
                 'killed_injury_severity_text': "הרוג",
                 'killed_injury_severity_count': 0 },
                {'year': 2010,
                 'light_injury_severity_text': "פצוע קל",
                 'light_injury_severity_count': 24,
                 'severe_injury_severity_text': "פצוע קשה",
                 'severe_injury_severity_count': 0,
                 'killed_injury_severity_text': "הרוג",
                 'killed_injury_severity_count': 1 },
                {'year': 2011,
                 'light_injury_severity_text': "פצוע קל",
                 'light_injury_severity_count': 9,
                 'severe_injury_severity_text': "פצוע קשה",
                 'severe_injury_severity_count': 2,
                 'killed_injury_severity_text': "הרוג",
                 'killed_injury_severity_count': 1 },
                {'year': 2012,
                 'light_injury_severity_text': "פצוע קל",
                 'light_injury_severity_count': 21,
                 'severe_injury_severity_text': "פצוע קשה",
                 'severe_injury_severity_count': 2,
                 'killed_injury_severity_text': "הרוג",
                 'killed_injury_severity_count': 4 },
                {'year': 2013,
                 'light_injury_severity_text': "פצוע קל",
                 'light_injury_severity_count': 21,
                 'severe_injury_severity_text': "פצוע קשה",
                 'severe_injury_severity_count': 2,
                 'killed_injury_severity_text': "הרוג",
                 'killed_injury_severity_count': 4 },
                {'year': 2014,
                 'light_injury_severity_text': "פצוע קל",
                 'light_injury_severity_count': 10,
                 'severe_injury_severity_text': "פצוע קשה",
                 'severe_injury_severity_count': 0,
                 'killed_injury_severity_text': "הרוג",
                 'killed_injury_severity_count': 1 },
                {'year': 2015,
                 'light_injury_severity_text': "פצוע קל",
                 'light_injury_severity_count': 13,
                 'severe_injury_severity_text': "פצוע קשה",
                 'severe_injury_severity_count': 2,
                 'killed_injury_severity_text': "הרוג",
                 'killed_injury_severity_count': 0 }]

    injured_accidents_with_pedestrians = Widget(
        WidgetId.injured_accidents_with_pedestrians,
        items=injured_accidents_with_pedestrians_mock_data(),
        text={"title": "נפגעים בתאונות עם הולכי רגל ברחוב ז׳בוטינסקי, פתח תקווה (2009-2015)"},
    )
    output["widgets"].append(injured_accidents_with_pedestrians.serialize())

    def accident_severity_by_cross_location_mock_data():  # Temporary for Frontend
        return [{'cross_location_text': "במעבר חצייה",
                 'light_injury_severity_text': "פצוע קל",
                 'light_injury_severity_count': 37,
                 'severe_injury_severity_text': "פצוע קשה",
                 'severe_injury_severity_count': 6,
                 'killed_injury_severity_text': "הרוג",
                 'killed_injury_severity_count': 0 },
                {'cross_location_text': "לא במעבר חצייה",
                 'light_injury_severity_text': "פצוע קל",
                 'light_injury_severity_count': 11,
                 'severe_injury_severity_text': "פצוע קשה",
                 'severe_injury_severity_count': 10,
                 'killed_injury_severity_text': "הרוג",
                 'killed_injury_severity_count': 0 }]

    accident_severity_by_cross_location = Widget(
        WidgetId.accident_severity_by_cross_location,
        items=accident_severity_by_cross_location_mock_data(),
        text={"comment": "בן יהודה תל אביב בין השנים 2008-2020"}
    )
    output["widgets"].append(accident_severity_by_cross_location.serialize())

    def motorcycle_accidents_vs_all_accidents_mock_data():  # Temporary for Frontend
        return [{
                "location": "כביש 20",
                "vehicle": "אופנוע",
                "percentage": 0.5
                },
                {
                "location": "כביש 20",
                "vehicle": "אחר",
                "percentage": 0.5
                },
                {
                "location": "כל הארץ",
                "vehicle": "אחר",
                "percentage": 0.802680566
                },
                {
                "location": "כל הארץ",
                "vehicle": "אופנוע",
                "percentage": 0.197319434
                }
                ]

    motorcycle_accidents_vs_all_accidents = Widget(
        WidgetId.motorcycle_accidents_vs_all_accidents,
        items=motorcycle_accidents_vs_all_accidents_mock_data(),
        text={"title": "אחוז תאונות אופנוע מכלל התאונות הקשות והקטלניות"},
    )
    output["widgets"].append(motorcycle_accidents_vs_all_accidents.serialize())

    def accidents_count_pedestrians_per_vehicle_street_vs_all_mock_data():  # Temporary for Frontend
        return [{
                "location": "כל הארץ",
                "vehicle": "מכונית",
                "num_of_accidents": 61307
                },
                {
                "location": "כל הארץ",
                "vehicle": "רכב כבד",
                "num_of_accidents": 15801
                },
                {
                "location": "כל הארץ",
                "vehicle": "אופנוע",
                "num_of_accidents": 3884
                },
                {
                "location": "כל הארץ",
                "vehicle": "אופניים וקורקינט ממונע",
                "num_of_accidents": 1867
                },
                {
                "location": "כל הארץ",
                "vehicle": "אחר",
                "num_of_accidents": 229
                },
                {
                "location": "בן יהודה",
                "vehicle": "מכונית",
                "num_of_accidents": 64
                },
                {
                "location": "בן יהודה",
                "vehicle": "אופנוע",
                "num_of_accidents": 40
                },
                {
                "location": "בן יהודה",
                "vehicle": "רכב כבד",
                "num_of_accidents": 22
                },
                {
                "location": "בן יהודה",
                "vehicle": "אופניים וקורקינט ממונע",
                "num_of_accidents": 9
                }
                ]

    accidents_count_pedestrians_per_vehicle_street_vs_all = Widget(
        WidgetId.accidents_count_pedestrians_per_vehicle_street_vs_all,
        items=accidents_count_pedestrians_per_vehicle_street_vs_all_mock_data(),
        text={"title": "פגיעות בהולכי רגל ברחוב בן יהודה בתל אביב לפי סוג רכב פוגע, בהשוואה לתאונות עירוניות בכל הארץ"},
    )
    output["widgets"].append(accidents_count_pedestrians_per_vehicle_street_vs_all.serialize())

    return json.dumps(output, default=str)


def get_infographics_data(news_flash_id, years_ago):
    try:
        res = infographics_data_cache_updater.get_infographics_data_from_cache(news_flash_id, years_ago)
        # temporary: add mocked data that is not included in cache
        request_params = get_request_params(news_flash_id, years_ago)
        res.append(generate_widgets(request_params, get_widgets_def(), False))
    except Exception as e:
        logging.error(
            f"Exception while retrieving from infographics cache({news_flash_id},{years_ago})"
            f":cause:{e.__cause__}, class:{e.__class__}"
        )
        res = {}
    if not res:
        logging.error(f"infographics_data({news_flash_id}, {years_ago}) not found in cache")
    return res
