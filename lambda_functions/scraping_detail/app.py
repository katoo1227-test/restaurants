import boto3
import os
import json
import requests
import time
import urllib.parse
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
import dynamodb_types
from dataclasses import dataclass


@dataclass
class IdParams:
    id: str


@dataclass
class Task:
    kind: str
    params_id: str
    params: IdParams


def lambda_handler(event, context):

    success_response = {
        "statusCode": 200,
        "body": "Process Complete",
    }

    try:

        # タスクの取得
        task = get_task()

        # 該当レコードがなければスケジュールを削除
        if task == {}:
            delete_schedule()
            return success_response

        # S3へ画像をアップロード
        put_images(task.params.id)

        # 詳細情報の取得
        info = get_detail_info(task.params.id)

        # 飲食店情報の更新
        update_restaurant(task.params.id, info)

        # タスクの削除
        delete_task(task.kind, task.params_id)
    except Exception as e:
        payload = {"function_name": context.function_name, "msg": str(e)}
        boto3.client("lambda").invoke(
            FunctionName=os.environ["ARN_LAMBDA_ERROR_COMMON"],
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )

    return success_response


def get_task() -> Task:
    """
    タスクの取得

    Returns
    -------
    Task
    """
    res = boto3.client("dynamodb").query(
        TableName=os.environ["NAME_DYNAMODB_TABLE_TASKS"],
        KeyConditionExpression="kind = :kind",
        ExpressionAttributeValues={
            ":kind": dynamodb_types.serialize(os.environ["NAME_TASK_SCRAPING_DETAIL"])
        },
        Limit=1,
    )

    # なければ空オブジェクトで返却
    if len(res["Items"]) == 0:
        return {}

    res = dynamodb_types.deserialize_dict(res["Items"][0])
    return Task(
        kind=res["kind"], params_id=res["params_id"], params=IdParams(**res["params"])
    )


def delete_schedule() -> None:
    """
    スケジュールの削除
    """
    payload = {"task": "delete", "name": os.environ["NAME_TASK_SCRAPING_DETAIL"]}
    boto3.client("lambda").invoke(
        FunctionName=os.environ["ARN_LAMBDA_HANDLER_SCHEDULES"],
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )


def put_images(id: str) -> None:
    """
    飲食店画像をS3へ保存

    Parameters
    ----------
    id: str
        飲食店ID
    """

    s3 = boto3.client("s3")

    # 写真一覧URL
    url = f"https://www.hotpepper.jp/str{id}/photo/"

    # HTMLを取得
    html = requests.get(url)

    # ステータスが404の場合は画像がないので既存の画像を削除
    if html.status_code == 404:
        # もともとなければ何もしない
        res = s3.list_objects_v2(
            Bucket=os.environ["NAME_IMAGES_BUCKET"], Prefix=f"images/{id}/"
        )
        if "Contents" not in res:
            return

        # フォルダごと削除
        delete_objects = [{"Key": obj["Key"]} for obj in res["Contents"]]
        s3.delete_objects(
            Bucket=os.environ["NAME_IMAGES_BUCKET"], Delete={"Objects": delete_objects}
        )
        return

    # HTML解析
    soup = BeautifulSoup(html.content, "html.parser")

    # 「.jsc-photo-list」での検索
    is_jsc_photo_list = False
    jsc_photo_list = soup.select(".jsc-photo-list")
    jsc_photo_list_len = len(jsc_photo_list)
    if jsc_photo_list_len != 0:
        is_jsc_photo_list = True

    # 「.jsc-photo-list-elm」での検索
    is_jsc_photo_list_elm = False
    jsc_photo_list_elm = soup.select(".jsc-photo-list-elm")
    jsc_photo_list_elm_len = len(jsc_photo_list_elm)
    if jsc_photo_list_elm_len != 0:
        is_jsc_photo_list_elm = True

    # いずれも取得できていなければエラー
    if is_jsc_photo_list == False and is_jsc_photo_list_elm == False:
        raise Exception(f"飲食店画像一覧の取得に失敗。{id}")

    # 画像URLを格納
    img_urls = []
    if is_jsc_photo_list:
        for i, elm in enumerate(jsc_photo_list):
            img_path = elm.get("data-src")
            if img_path is None:
                raise Exception(f"飲食店画像URLの取得に失敗。{str(elm)}")
            img_urls.append(f"https://www.hotpepper.jp{img_path}")
    if is_jsc_photo_list_elm:
        img_urls = [e.get("data-src") for e in jsc_photo_list_elm]

    # 画像を取得して保存
    img_urls_len = len(img_urls)
    for i, url in enumerate(img_urls):
        image = requests.get(url)
        if image.status_code != 200:
            raise Exception(f"飲食店画像の取得に失敗。id: {id}, url: {url}")
        _, ext = os.path.splitext(url)
        boto3.client("s3").put_object(
            Bucket=os.environ["NAME_IMAGES_BUCKET"],
            Key=f"images/{id}/{i + 1}{ext}",
            Body=image.content,
        )

        # 最後でなければ1秒待つ
        if i + 1 != img_urls_len:
            time.sleep(1)


def get_detail_info(id: str) -> dict:
    """
    詳細情報を取得

    Parameters
    ----------
    id: str
        飲食店ID

    Returns
    -------
    dict
        name: str
            飲食店名
        genre: str
            ジャンル
        sub_genre: str
            サブジャンル
        address: str
            住所
        latitude: float
            緯度
        longitude: float
            経度
        open_hours: str
            営業時間
        close_days: str
            定休日情報
        parking: str
            駐車場
    """
    # 返り値の初期化
    result = {
        "name": "",
        "genre": "",
        "sub_genre": "",
        "address": "",
        "latitude": 0,
        "longitude": 0,
        "open_hours": "",
        "close_days": "",
        "parking": "",
    }

    # URL
    url = f"https://www.hotpepper.jp/str{id}"

    # HTML解析
    html = requests.get(url)
    soup = BeautifulSoup(html.content, "html.parser")

    # 飲食店名
    name_dom = soup.select_one("h1.shopName")
    if name_dom is None:
        raise Exception(f"飲食店名の取得失敗。{url}")
    result["name"] = name_dom.text

    # ジャンル・サブジャンル
    section_blocks = soup.select(".jscShopInfoInnerSection .shopInfoInnerSectionBlock")
    if len(section_blocks) == 0:
        raise Exception(f"ジャンル・サブジャンルの取得失敗。{url}")
    for dl_tag in section_blocks:
        dt = dl_tag.select_one("dt")
        if dt is None:
            raise Exception(
                f"ジャンル・サブジャンルのセクションタイトルの取得失敗。{url}\n{str(dl_tag)}"
            )

        # ジャンルでなければスキップ
        if dt.text != "ジャンル":
            continue

        genre_titles_dom = dl_tag.select(".shopInfoInnerItemTitle")
        genre_titles_dom_len = len(genre_titles_dom)
        if genre_titles_dom_len == 0:
            raise Exception(
                f"ジャンル・サブジャンルの値の取得失敗。{url}\n「.shopInfoInnerItemTitle」が存在しない。"
            )

        # ジャンル
        genre_link = genre_titles_dom[0].select_one("a")
        if genre_link is None:
            raise Exception(f"ジャンルの値の取得失敗。{url}\n<a>が存在しない。")
        result["genre"] = genre_link.text.strip()

        # サブジャンル
        if genre_titles_dom_len == 2:
            sub_genre_link = genre_titles_dom[1].select_one("a")
            if sub_genre_link is None:
                raise Exception(f"サブジャンルの値の取得失敗。{url}\n<a>が存在しない。")
            result["sub_genre"] = sub_genre_link.text.strip()

    # 住所・営業時間・定休日
    info_tables = soup.select(".infoTable")
    if len(info_tables) == 0:
        raise Exception(
            f"住所・営業時間・定休日の取得失敗。{url}\nページ下部に詳細情報がない。"
        )
    for table in info_tables:

        # 飲食店情報の表でなければスキップ
        if table["summary"] != "お店情報" and table["summary"] != "設備":
            continue

        # 行の取得
        trs = table.select("tr")
        if len(trs) == 0:
            raise Exception(f"お店情報の表に行がない。{url}")

        # 項目名
        for tr in trs:
            th = tr.select_one("th")
            if th is None:
                raise Exception(f"お店情報に項目がない行がある。{url}")

            # 取得対象でなければスキップ
            item_name = th.text.strip()
            if item_name not in ["住所", "営業時間", "定休日"]:
                continue

            td = tr.select_one("td")
            if td is None:
                raise Exception(f"住所の取得に失敗。{url}\n{str(tr)}")

            # 住所
            if item_name == "住所":
                result["address"] = td.text.strip()

            # 営業時間
            if item_name == "営業時間":
                result["open_hours"] = td.decode_contents(formatter="html").strip()

            # 定休日
            if item_name == "定休日":
                result["close_days"] = td.decode_contents(formatter="html").strip()

            # 駐車場
            if item_name == "駐車場":
                result["parking"] = td.text.strip()

    # 緯度・経度
    if result["address"] != "":
        # Google Geocoding APIで住所から緯度・経度を取得
        res = boto3.client("ssm").get_parameter(
            Name=os.environ["NAME_PARAMETER_API_KEY_GOOGLE_GEOCODING"],
            WithDecryption=True,
        )
        url_params = {"address": result["address"], "key": res["Parameter"]["Value"]}
        url = f"https://maps.googleapis.com/maps/api/geocode/json?{urllib.parse.urlencode(url_params)}"
        response = requests.get(url)

        # 正しく取得できていなければ例外を投げる
        data = response.json()
        try:
            lat = data["results"][0]["geometry"]["location"]["lat"]
            lng = data["results"][0]["geometry"]["location"]["lng"]
        except KeyError:
            raise Exception(f"緯度経度の取得に失敗。\n{id}\n{url}")

        result["latitude"] = lat
        result["longitude"] = lng

    return result


def update_restaurant(id: str, info: dict) -> None:
    """
    飲食店テーブルの更新

    Parameters
    ----------
    id: str
        飲食店ID
    info: dict
        name: str
            飲食店名
        genre: str
            ジャンル
        sub_genre: str
            サブジャンル
        address: str
            住所
        latitude: float
            緯度
        longitude: float
            経度
        open_hours: str
            営業時間
        close_days: str
            定休日情報
        parking: str
            駐車場
    """
    dynamodb = boto3.client("dynamodb")
    res = dynamodb.get_item(
        TableName=os.environ["NAME_DYNAMODB_RESTAURANTS"],
        Key={"id": dynamodb_types.serialize(id)},
    )
    if "Item" not in res:
        raise Exception(f"飲食店データの取得に失敗。{id}のレコードが存在しない。")
    res_json = {k: dynamodb_types.deserialize(v) for k, v in res["Item"].items()}

    # 1つでもカラムの値が違っていればput対象
    is_put = False
    update_columns = [
        "name",
        "genre",
        "sub_genre",
        "address",
        "latitude",
        "longitude",
        "open_hours",
        "close_days",
        "parking",
    ]
    for c in update_columns:
        if c not in res_json or info[c] != res_json[c]:
            is_put = True
            break

    # put対象の場合
    if is_put:
        tz = pytz.timezone("Asia/Tokyo")
        now = datetime.now(tz)
        item = {
            "id": id,
            "name": info["name"],
            "large_service_area_code": res_json["large_service_area_code"],
            "large_service_area_name": res_json["large_service_area_name"],
            "service_area_code": res_json["service_area_code"],
            "service_area_name": res_json["service_area_name"],
            "large_area_code": res_json["large_area_code"],
            "large_area_name": res_json["large_area_name"],
            "middle_area_code": res_json["middle_area_code"],
            "middle_area_name": res_json["middle_area_name"],
            "small_area_code": res_json["small_area_code"],
            "small_area_name": res_json["small_area_name"],
            "genre": info["genre"],
            "sub_genre": info["sub_genre"],
            "address": info["address"],
            "latitude": info["latitude"],
            "longitude": info["longitude"],
            "open_hours": info["open_hours"],
            "close_days": info["close_days"],
            "parking": info["parking"],
            "is_notified": res_json["is_notified"],
            "created_at": res_json["created_at"],
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
        dynamodb.put_item(
            TableName=os.environ["NAME_DYNAMODB_RESTAURANTS"],
            Item=dynamodb_types.serialize_dict(item),
        )


def delete_task(kind: str, params_id: str) -> None:
    """
    タスクの削除

    Parameters
    ----------
    kind: str
        タスクの種類

    params_id: str
        パラメータID
    """
    boto3.client("dynamodb").delete_item(
        TableName=os.environ["NAME_DYNAMODB_TABLE_TASKS"],
        Key=dynamodb_types.serialize_dict({"kind": kind, "params_id": params_id}),
    )
