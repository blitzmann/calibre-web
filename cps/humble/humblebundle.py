from __future__ import division, print_function, unicode_literals
from time import time
import requests
from flask_login import current_user


from flask import Blueprint, jsonify, request, abort
from cps.humble.tasks.download import TaskDownloadBooks
from cps.humble.tasks.linker import TaskDownloadLinker
from cps.humble.tasks.orders import TaskGetOrders
from cps.helper import localize_task_status

try:
    from googleapiclient.errors import HttpError
except ImportError:
    pass

from cps import logger
from cps.services.worker import WorkerThread
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
    tasks = WorkerThread.getInstance().tasks
    data = request.get_json()

    queuedTask = next(filter(lambda x: str(x.task.id) == data["task_id"], tasks))

    if queuedTask is None:
        abort(404)

    return jsonify(
        {
            "status": localize_task_status(queuedTask.task.stat),
            "progress": queuedTask.task.progress,
            "results": queuedTask.task.results
         }
    )

@humble.route("/ajax/submit", methods=['POST'])
def submit_downloads():
    data = request.get_json()
    for bundle in data:
        bundle_name = bundle["name"]
        for product in bundle["products"]:
            product_name = product["name"]
            task_ids = []

            for download_info in product["downloads"]:
                task = TaskDownloadBooks(
                    task_message='{book_title} ({size} {format}) [{bundle_name}]'.format(
                        book_title=(product_name[:70] + '...') if len(product_name) > 70 else product_name,
                        size=download_info["human_size"],
                        format=download_info["name"],
                        bundle_name=bundle_name),
                    bundle_name=bundle_name,
                    product_name=product_name,
                    download_info=download_info
                )

                task_ids.append(task.id)
                WorkerThread.add(current_user.nickname, task)

            # Here we create a special task that takes the download tasks and gathers the meta about them and links them under one book
            WorkerThread.add(current_user.nickname, TaskDownloadLinker(
                task_message='{book_title} [{bundle_name}]'.format(
                    book_title=(product_name[:70] + '...') if len(product_name) > 70 else product_name,
                    bundle_name=bundle_name
                ),
                bundle_name=bundle_name,
                product_name=product_name,
                tasks=task_ids
            ))
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
    orders = requests.get('https://www.humblebundle.com/api/v1/user/order?ajax=true', headers=headers, cookies=cookies)

    if orders.status_code == 401:
        return jsonify({"success": False, "error": "Authentication error. Double check token and try again."})
    if not orders.ok:
        log.error("Error getting orders. Status code: {}. Data: {}".format(orders.status_code, orders.json()))
        return jsonify({"success": False, "error": "There was an unknown error getting."})

    orders = orders.json()

    task = TaskGetOrders(data["auth"], orders)
    WorkerThread.add(current_user.nickname, task)
    return jsonify({
        "task_id": task.id,
        "success": True
    })


@humble.route("/")
def humble_set_up():
    # return render_template('test.html')
    return render_title_template(
        'humble.html',
        title="Humble Downloader",
        # this is so that caliBlur! theme plays well with our DOM stuff without having to override a bunch of :poop:
        bodyClass="admin"
    )

