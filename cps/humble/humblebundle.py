from __future__ import division, print_function, unicode_literals
from time import time
import requests
from flask_login import current_user


from flask import Blueprint, jsonify, flash, request, redirect, url_for, abort, render_template
from cps import worker
from cps.worker import STAT_WAITING, STAT_FAIL, STAT_STARTED, STAT_FINISH_SUCCESS
from cps.helper import localize_task_status

try:
    from googleapiclient.errors import HttpError
except ImportError:
    pass

from cps import logger, config, ub, calibre_db
from cps.web import admin_required, render_title_template

humble = Blueprint('humble', __name__, template_folder='templates', static_folder='static')
log = logger.create()

current_milli_time = lambda: int(round(time() * 1000))

def normalizeFormat(format):
    lc = format.toLowerCase()
    if lc == '.cbz':
      return 'cbz'
    if lc == 'pdf (hq)' or lc == 'pdf (hd)':
      return 'pdf_hd'
    if lc == 'download':
      return 'pdf'
    return lc

@humble.route("/ajax/task/progress", methods=['POST'])
def get_task_status():
    tasks = worker.get_taskstatus()
    data = request.get_json()

    task = next(filter(lambda x: x["id"] == data["task_id"], tasks))

    if task is None:
        abort(404)

    return jsonify(
        {
            "status": localize_task_status(task["stat"]),
            "progress": task["progress"],
            "results": task["results"]
         }
    )

@humble.route("/ajax/submit", methods=['POST'])
def submit_downloads():
    data = request.get_json()
    for bundle in data:
        for product in bundle["products"]:
            worker.add_hb_download(current_user.nickname, bundle["name"], product["name"], product["downloads"])
    return jsonify({"sucess": True})

@humble.route("/ajax/orders", methods=['POST'])
def get_orders():
    data = request.get_json()

    cookies = {
        '_simpleauth_sess': data["auth"]
    }

    headers = {
        'Accept': 'application/json',
        'Accept-Charset': 'utf-8',
        'User-Agent': 'calibre-web-hb-downloader',
    }

    # todo: actually handle a bad token coming in. This won't just #yoloFail for us :S
    orders = requests.get('https://www.humblebundle.com/api/v1/user/order?ajax=true', headers=headers, cookies=cookies).json()

    task_id = worker.hb_get_orders(current_user.nickname, data["auth"], orders)
    return jsonify({"task_id": task_id})


@humble.route("/")
def humble_set_up():
    # return render_template('test.html')
    return render_title_template(
        'humble.html',
        title="Humble Downloader",
        # this is so that caliBlur! theme plays well with our DOM stuff without having to override a bunch of :poop:
        bodyClass="admin"
    )

