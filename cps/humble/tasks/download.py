from __future__ import division, print_function, unicode_literals
import requests
import os
import hashlib
from uuid import uuid4
from urllib.parse import urlparse
from tempfile import gettempdir

from cps import constants
from cps.services.worker import CalibreTask
from cps import logger
from cps.services.worker import STAT_STARTED

log = logger.create()

BUF_SIZE = 65536


class TaskDownloadBooks(CalibreTask):
    def __init__(self, task_message, bundle_name, product_name, download_info):
        super(TaskDownloadBooks, self).__init__(task_message)
        self.bundle_name = bundle_name
        self.product_name = product_name
        self.download_info = download_info

        self.results = dict()

    def run(self, worker_thread):
        try:
            dl = self.download_info

            tmp_dir = os.path.join(gettempdir(), 'calibre_web')

            if dl["name"].lower() not in constants.EXTENSIONS_UPLOAD:
                raise Exception("File extension '{ext}' is not allowed to be uploaded to this server".format(
                    ext=dl["name"].lower()))

            if not os.path.isdir(tmp_dir):
                os.mkdir(tmp_dir)

            id = dl.get("sha1", None) or dl.get("md5", None) or uuid4()

            tmp_file_path = os.path.join(tmp_dir, id)
            log.debug("Temporary file: %s", tmp_file_path)

            url = dl["url"]["web"]
            a = urlparse(url)
            filename = os.path.basename(a.path)
            filename_root, file_extension = os.path.splitext(filename)

            self.results = {
                "filepath": tmp_file_path,
                "filename_root": filename_root,
                "file_extension": file_extension
            }

            # download file to temp location, complete with progress loop
            with open(tmp_file_path, 'wb') as f:
                response = requests.get(dl["url"]["web"], stream=True)
                total = response.headers.get('content-length')

                if total is None:
                    f.write(response.content)
                else:
                    downloaded = 0
                    total = int(total)
                    for data in response.iter_content(chunk_size=max(int(total / 1000), 1024 * 1024)):
                        downloaded += len(data)
                        f.write(data)
                        self.progress = downloaded / total

            # Determine checksum
            # todo: determine which hash the downloads have, and try all of them. If at least one works, then it's fine.
            md5 = hashlib.md5()

            with open(tmp_file_path, 'rb') as f:
                while True:
                    data = f.read(BUF_SIZE)
                    if not data:
                        break
                    md5.update(data)

            # Commenting out for now, apparently Humble's api can return inaccurate data for the hashes
            # if md5.hexdigest() != dl["md5"]:
            #     return self._handleError("Integrity check faailed after download")

            self.success = True
            self._handleSuccess()

            # it's technically done, but the link portion needs these around still. We set these to started to avoid
            # the cleanup process from removing them

            self.stat = STAT_STARTED
        except Exception as e:
            self._handleError(str(e))

    @property
    def name(self):
        return "Humble: Get Orders"
