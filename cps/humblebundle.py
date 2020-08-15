from __future__ import division, print_function, unicode_literals
from time import time
import requests
from flask_login import current_user


from flask import Blueprint, jsonify, flash, request, redirect, url_for, abort
from . import worker


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

    # orders = requests.get('https://www.humblebundle.com/api/v1/user/order?ajax=true', headers=headers, cookies=cookies).json()
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

    for bundle in ret:
        for product in bundle["products"]:
            worker.add_hb_download(current_user.nickname, bundle["name"], product["name"], product["downloads"])

    return jsonify(ret)

