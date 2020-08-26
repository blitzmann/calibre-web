from __future__ import division, print_function, unicode_literals
import requests

from cps.services.worker import CalibreTask
from cps import logger
log = logger.create()


class TaskGetOrders(CalibreTask):
    def __init__(self, auth_token, keys):
        super(TaskGetOrders, self).__init__("Getting Orders")
        self.auth_token = auth_token
        self.keys = keys

        self.results = dict()

    def run(self, worker_thread):
        headers = {
            'Accept': 'application/json',
            'Accept-Charset': 'utf-8',
            'User-Agent': 'calibre-web-hb-downloader',
        }

        cookies = {
            '_simpleauth_sess': self.auth_token
        }

        # clear the auth token so that the GC kills it
        self.auth_token = None

        def filter_structs(download):
            if not download["name"] or not download["url"]:
                return False
            return True

        try:
            keys = self.keys

            ret = []
            for i, order in enumerate(keys):
                # todo: retry logic / handle error
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

                self.progress = (i + 1) / len(keys)

            self.results = ret
            self._handleSuccess()
        except Exception as e:
            self._handleError(str(e))

    @property
    def name(self):
        return "Humble: Get Orders"
