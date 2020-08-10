from __future__ import division, print_function, unicode_literals
import os
import sys
import hashlib
import json
import tempfile
from urllib.parse import urlparse
from uuid import uuid4
from time import time
from shutil import move, copyfile
import requests

from flask import Blueprint, jsonify, flash, request, redirect, url_for, abort
from flask_babel import gettext as _
from flask_login import login_required
from . import worker
from .constants import BookMeta
from .editbooks import _add_to_db
from .uploader import process


try:
    from googleapiclient.errors import HttpError
except ImportError:
    pass

from . import logger, config, ub, calibre_db
from .web import admin_required


humble = Blueprint('humbledownload', __name__)
log = logger.create()

current_milli_time = lambda: int(round(time() * 1000))

headers = {
    'Accept': 'application/json',
    'Accept-Charset': 'utf-8',
    'User-Agent': 'calibre-web-hb-downloader',
}


def normalizeFormat(format):
    lc = format.toLowerCase()
    if lc == '.cbz':
      return 'cbz'
    if lc == 'pdf (hq)' or lc == 'pdf (hd)':
      return 'pdf_hd'
    if lc == 'download':
      return 'pdf'
    return lc

def filter_structs(download):
    if not download["name"] or not download["url"]:
        return False
    return True

@humble.route("/hb/orders", methods=['POST'])
def get_hb_orders():
    data = request.get_json()
    cookies = {
        '_simpleauth_sess': data["auth"]
    }

    orders = requests.get('https://www.humblebundle.com/api/v1/user/order?ajax=true', headers=headers, cookies=cookies).json()
    orders = [{"gamekey":"3w74zkZYEK3ncyhY"}] # testing

    ret = []
    for order in orders:
        req = requests.get(
            'https://www.humblebundle.com/api/v1/order/{}?ajax=true'.format(order["gamekey"]),
            headers=headers,
            cookies=cookies)
        bundle = req.json()

        bundleRet = {
            "name": bundle["product"]["human_name"],
            "products": []
        }

        for product in bundle["subproducts"]:
            productRet = {
                "name": product["human_name"],
                "downloads": []
            }
            for dl in product["downloads"]:
                if dl["platform"] != 'ebook':
                    continue

                # this product has an ebook download
                productRet["downloads"].extend(
                    filter(filter_structs, dl["download_struct"])
                )
            if len(productRet["downloads"]) > 0:
                bundleRet["products"].append(productRet)

        if len(bundleRet["products"]) > 0:
            ret.append(bundleRet)


    ### testing here, thiss will move off to another route after user selects which bundles.
    for bundle in ret:
        theman = [next(obj for obj in bundle["products"] if obj["name"] == "The Man Who Cried I Am")]
        for product in theman: #bundle["products"][:2]:

            # ###### TEST
            # results = []
            #
            # for dl in product["downloads"]:
            #     BUF_SIZE = 65536  # lets read stuff in 64kb chunks!
            #
            #     tmp_dir = os.path.join(tempfile.gettempdir(), 'calibre_web')
            #
            #     if not os.path.isdir(tmp_dir):
            #         os.mkdir(tmp_dir)
            #
            #     tmp_file_path = os.path.join(tmp_dir, dl["sha1"])
            #     log.debug("Temporary file: %s", tmp_file_path)
            #
            #     url = dl["url"]["web"]
            #     a = urlparse(url)
            #     filename = os.path.basename(a.path)
            #     filename_root, file_extension = os.path.splitext(filename)
            #
            #     # todo: filename should be in temp directory... unsure what it should be called... look into the upload route to
            #     # determine how this happens
            #     with open(tmp_file_path, 'wb') as f:
            #         response = requests.get(dl["url"]["web"], stream=True)
            #         total = response.headers.get('content-length')
            #
            #         if total is None:
            #             f.write(response.content)
            #         else:
            #             downloaded = 0
            #             total = int(total)
            #             for data in response.iter_content(chunk_size=max(int(total / 1000), 1024 * 1024)):
            #                 downloaded += len(data)
            #                 f.write(data)
            #         pass
            #
            #     sha1 = hashlib.sha1()
            #
            #     with open(tmp_file_path, 'rb') as f:
            #         while True:
            #             data = f.read(BUF_SIZE)
            #             if not data:
            #                 break
            #             sha1.update(data)
            #
            #     results.append({
            #         "success": True,  # todo: don't hardcode this
            #         "filepath": tmp_file_path,
            #         "filename_root": filename_root,
            #         "file_extension": file_extension,
            #         "dl": dl
            #     })
            #
            # processed = []
            # # we loop through the DL items start assigning their meta data to the base meta object
            # for task in results:
            #     processed.append(process(
            #         task["filepath"],
            #         task["filename_root"],
            #         task["file_extension"],
            #         ''  # todo: figure this guy out
            #     ))
            #
            # # create a new BaseMeta tuple that holds the final values that we will use
            # base = BookMeta(
            #     file_path=processed[0].file_path,
            #     extension=processed[0].extension,
            #     # We know the title from the bundle itself. Hardcode it here to avoid issue where we can't get any metadata, and thus don't know what book this is
            #     title=product["name"],
            #     author=next((meta.author for meta in processed if meta.author != _(u'Unknown')), None) or _(u'Unknown'),
            #     cover=next((meta.cover for meta in processed if meta.cover is not None), None),
            #     description=next((meta.description for meta in processed if meta.description not in (None, "")),
            #                      None) or "",
            #     tags=next((meta.tags for meta in processed if meta.tags not in (None, "")), None) or "",
            #     series=next((meta.series for meta in processed if meta.series not in (None, "")), None) or "",
            #     series_id=next((meta.series_id for meta in processed if meta.series_id not in (None, "")), None) or "",
            #     languages=next((meta.languages for meta in processed if meta.languages not in (None, "")), None) or ""
            # )
            #
            # try:
            #     results, db_book, error = _add_to_db(base, calibre_db)
            # except Exception as e:
            #     print("HO BOY IT'S STILL FUCKED")
            # ###### /TEST

            worker.add_hb_download('TEST USERNAME!', bundle["name"], product["name"], product["downloads"])

    return jsonify(ret)
