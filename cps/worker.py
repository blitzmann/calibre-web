# -*- coding: utf-8 -*-

#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#    Copyright (C) 2018-2019 OzzieIsaacs, bodybybuddha, janeczku
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program. If not, see <http://www.gnu.org/licenses/>.

from __future__ import division, print_function, unicode_literals
import sys
import os
import re
import smtplib
import socket
import time
import math
import threading
import requests
import hashlib
from tempfile import gettempdir
from urllib.parse import urlparse

from .constants import BookMeta


try:
    import queue
except ImportError:
    import Queue as queue
from glob import glob
from shutil import copyfile
from datetime import datetime

try:
    from StringIO import StringIO
    from email.MIMEBase import MIMEBase
    from email.MIMEMultipart import MIMEMultipart
    from email.MIMEText import MIMEText
except ImportError:
    from io import StringIO
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

from email import encoders
from email.utils import formatdate
from email.utils import make_msgid
from email.generator import Generator
from flask_babel import gettext as _

from . import calibre_db, db
from . import logger, config
from .subproc_wrapper import process_open
from . import gdriveutils
from .uploader import process

log = logger.create()

BUF_SIZE = 65536  # lets read stuff in 64kb chunks!

chunksize = 8192
# task 'status' consts
STAT_WAITING = 0
STAT_FAIL = 1
STAT_STARTED = 2
STAT_FINISH_SUCCESS = 3
#taskType consts
TASK_EMAIL = 1
TASK_CONVERT = 2
TASK_UPLOAD = 3
TASK_CONVERT_ANY = 4
TASK_HB_DOWNLOAD = 5
TASK_HB_LINK = 6
TASK_TEST = 7

RET_FAIL = 0
RET_SUCCESS = 1


def _get_main_thread():
    for t in threading.enumerate():
        if t.__class__.__name__ == '_MainThread':
            return t
    raise Exception("main thread not found?!")


# For gdrive download book from gdrive to calibredir (temp dir for books), read contents in both cases and append
# it in MIME Base64 encoded to
def get_attachment(bookpath, filename):
    """Get file as MIMEBase message"""
    calibrepath = config.config_calibre_dir
    if config.config_use_google_drive:
        df = gdriveutils.getFileFromEbooksFolder(bookpath, filename)
        if df:
            datafile = os.path.join(calibrepath, bookpath, filename)
            if not os.path.exists(os.path.join(calibrepath, bookpath)):
                os.makedirs(os.path.join(calibrepath, bookpath))
            df.GetContentFile(datafile)
        else:
            return None
        file_ = open(datafile, 'rb')
        data = file_.read()
        file_.close()
        os.remove(datafile)
    else:
        try:
            file_ = open(os.path.join(calibrepath, bookpath, filename), 'rb')
            data = file_.read()
            file_.close()
        except IOError as e:
            log.exception(e)
            log.error(u'The requested file could not be read. Maybe wrong permissions?')
            return None

    attachment = MIMEBase('application', 'octet-stream')
    attachment.set_payload(data)
    encoders.encode_base64(attachment)
    attachment.add_header('Content-Disposition', 'attachment',
                          filename=filename)
    return attachment


# Class for sending email with ability to get current progress
class emailbase():

    transferSize = 0
    progress = 0

    def data(self, msg):
        self.transferSize = len(msg)
        (code, resp) = smtplib.SMTP.data(self, msg)
        self.progress = 0
        return (code, resp)

    def send(self, strg):
        """Send `strg' to the server."""
        log.debug('send: %r', strg[:300])
        if hasattr(self, 'sock') and self.sock:
            try:
                if self.transferSize:
                    lock=threading.Lock()
                    lock.acquire()
                    self.transferSize = len(strg)
                    lock.release()
                    for i in range(0, self.transferSize, chunksize):
                        if isinstance(strg, bytes):
                            self.sock.send((strg[i:i+chunksize]))
                        else:
                            self.sock.send((strg[i:i + chunksize]).encode('utf-8'))
                        lock.acquire()
                        self.progress = i
                        lock.release()
                else:
                    self.sock.sendall(strg.encode('utf-8'))
            except socket.error:
                self.close()
                raise smtplib.SMTPServerDisconnected('Server not connected')
        else:
            raise smtplib.SMTPServerDisconnected('please run connect() first')

    @classmethod
    def _print_debug(self, *args):
        log.debug(args)

    def getTransferStatus(self):
        if self.transferSize:
            lock2 = threading.Lock()
            lock2.acquire()
            value = int((float(self.progress) / float(self.transferSize))*100)
            lock2.release()
            return str(value) + ' %'
        else:
            return "100 %"


# Class for sending email with ability to get current progress, derived from emailbase class
class email(emailbase, smtplib.SMTP):

    def __init__(self, *args, **kwargs):
        smtplib.SMTP.__init__(self, *args, **kwargs)


# Class for sending ssl encrypted email with ability to get current progress, , derived from emailbase class
class email_SSL(emailbase, smtplib.SMTP_SSL):

    def __init__(self, *args, **kwargs):
        smtplib.SMTP_SSL.__init__(self, *args, **kwargs)


#Class for all worker tasks in the background
class WorkerThread(threading.Thread):

    def __init__(self):
        threading.Thread.__init__(self)
        self.status = 0
        self.current = 0
        self.last = 0
        self.queue = list()
        self.UIqueue = list()
        self.asyncSMTP = None
        self.id = 0
        self.db_queue = queue.Queue()
        calibre_db.add_queue(self.db_queue)
        self.doLock = threading.Lock()

    # Main thread loop starting the different tasksdb_queue
    def run(self):
        main_thread = _get_main_thread()
        while main_thread.is_alive():
            try:
                self.doLock.acquire()
                if self.current != self.last:
                    index = self.current
                    self.doLock.release()
                    if self.queue[index]['taskType'] == TASK_EMAIL:
                        self._send_raw_email()
                    if self.queue[index]['taskType'] == TASK_CONVERT:
                        self._convert_any_format()
                    if self.queue[index]['taskType'] == TASK_CONVERT_ANY:
                        self._convert_any_format()
                    if self.queue[index]['taskType'] == TASK_HB_DOWNLOAD:
                        self._download_hb()
                    if self.queue[index]['taskType'] == TASK_HB_LINK:
                        self._link_hb()
                    if self.queue[index]['taskType'] == TASK_TEST:
                        self.format_test()

                    # TASK_UPLOAD is handled implicitly
                    self.doLock.acquire()
                    self.current += 1
                    if self.current > self.last:
                        self.current = self.last
                    self.doLock.release()
                else:
                    self.doLock.release()
            except Exception as e:
                log.exception(e)
                self.doLock.release()
            if main_thread.is_alive():
                time.sleep(1)

    def get_send_status(self):
        if self.asyncSMTP:
            return self.asyncSMTP.getTransferStatus()
        else:
            return "0 %"

    def _delete_completed_tasks(self):
        for index, task in reversed(list(enumerate(self.UIqueue))):
            # Check if status is in any of the "Completed" things
            if task['stat'] in (STAT_FINISH_SUCCESS, STAT_FAIL):
                # delete tasks
                self.queue.pop(index)
                self.UIqueue.pop(index)
                # if we are deleting entries before the current index, adjust the index
                if index <= self.current and self.current:
                    self.current -= 1
        self.last = len(self.queue)

    def get_taskstatus(self):
        self.doLock.acquire()
        if self.current  < len(self.queue):
            if self.UIqueue[self.current]['stat'] == STAT_STARTED:
                if self.queue[self.current]['taskType'] == TASK_EMAIL:
                    self.UIqueue[self.current]['progress'] = self.get_send_status()
                self.UIqueue[self.current]['formRuntime'] = datetime.now() - self.queue[self.current]['starttime']
                self.UIqueue[self.current]['rt'] = self.UIqueue[self.current]['formRuntime'].days*24*60 \
                                                   + self.UIqueue[self.current]['formRuntime'].seconds \
                                                   + self.UIqueue[self.current]['formRuntime'].microseconds
        self.doLock.release()
        return self.UIqueue

    def _download_hb(self):
        # convert book, and upload in case of google drive
        import os
        self.doLock.acquire()

        index = self.current
        self.doLock.release()
        self.UIqueue[index]['stat'] = STAT_STARTED
        self.queue[index]['starttime'] = datetime.now()
        self.UIqueue[index]['formStarttime'] = self.queue[index]['starttime']
        curr_task = self.queue[index]['taskType']

        dl = self.queue[index]['download_info']

        tmp_dir = os.path.join(gettempdir(), 'calibre_web')

        if not os.path.isdir(tmp_dir):
            os.mkdir(tmp_dir)

        tmp_file_path = os.path.join(tmp_dir, dl["sha1"])
        log.debug("Temporary file: %s", tmp_file_path)

        url = dl["url"]["web"]
        a = urlparse(url)
        filename = os.path.basename(a.path)
        filename_root, file_extension = os.path.splitext(filename)

        # todo: filename should be in temp directory... unsure what it should be called... look into the upload route to
        # determine how this happens
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
                    self.UIqueue[index]['progress'] = '{} %'.format(math.floor((downloaded / total) * 100))
            pass

        sha1 = hashlib.sha1()

        with open(tmp_file_path, 'rb') as f:
            while True:
                data = f.read(BUF_SIZE)
                if not data:
                    break
                sha1.update(data)

        if sha1.hexdigest() != dl["sha1"]:
            return self._handleError("SHA1 integrity check failed after download")

        self.queue[index]["results"] = {
            "success": True, # todo: don't hardcode this
            "filepath": tmp_file_path,
            "filename_root": filename_root,
            "file_extension": file_extension
        }

        self._handleSuccess()

        # it's technically done, but the link portion needs these around still. We set these to started to avoid
        # the cleanup process from cleaning them up

        self.UIqueue[index]['stat'] = STAT_STARTED

    def _link_hb(self):
        from .editbooks import _add_to_db # importing it here prevents a circular import issue
        # convert book, and upload in case of google drive
        self.doLock.acquire()

        index = self.current
        self.doLock.release()
        self.UIqueue[index]['stat'] = STAT_STARTED
        self.queue[index]['starttime'] = datetime.now()
        self.UIqueue[index]['formStarttime'] = self.queue[index]['starttime']
        curr_task = self.queue[index]['taskType']

        task_ids = self.queue[index]['tasks']
        bundle_name = self.queue[index]['bundle_name']
        product_name = self.queue[index]['product_name']
        #
        # # se tup temp data
        # tmp_dir = os.path.join(gettempdir(), 'calibre_web')
        #
        # if not os.path.isdir(tmp_dir):
        #     os.mkdir(tmp_dir)
        #
        # filename = uploadfile.filename
        # filename_root, file_extension = os.path.splitext(filename)
        # md5 = hashlib.md5(filename.encode('utf-8')).hexdigest()
        # tmp_file_path = os.path.join(tmp_dir, md5)
        # log.debug("Temporary file: %s", tmp_file_path)
        # uploadfile.save(tmp_file_path)
        # return process(tmp_file_path, filename_root, file_extension, rarExcecutable)

        # find all the objects with these tasks
        dl_tasks = [x for x in self.queue if x.get("id", None) in task_ids]

        processed = []
        # we loop through the DL items start assigning their meta data to the base meta object
        for task in dl_tasks:
            processed.append(process(
                task["results"]["filepath"],
                task["results"]["filename_root"],
                task["results"]["file_extension"],
                ''  # todo: figure this guy out
            ))

        # create a new BaseMeta tuple that holds the final values that we will use
        meta = BookMeta(
            file_path=processed[0].file_path,
            extension=processed[0].extension,
            # We know the title from the bundle itself. Hardcode it here to avoid issue where we can't get any metadata, and thus don't know what book this is
            title=product_name,
            author=next((meta.author for meta in processed if meta.author != _(u'Unknown')), None) or _(u'Unknown'),
            cover=next((meta.cover for meta in processed if meta.cover is not None), None),
            description=next((meta.description for meta in processed if meta.description not in (None, "")), None) or "",
            tags=next((meta.tags for meta in processed if meta.tags not in (None, "")), None) or "",
            series=next((meta.series for meta in processed if meta.series not in (None, "")), None) or "",
            series_id=next((meta.series_id for meta in processed if meta.series_id not in (None, "")), None) or "",
            languages=next((meta.languages for meta in processed if meta.languages not in (None, "")), None) or ""
        )

        task = {'task': 'add_book', 'meta': meta, 'id': self.UIqueue[index]["id"]}
        self.db_queue.put(task)
        # todo: if there's an error, call back to this thread with details using the id

        # try:
        #     results, db_book, error = _add_to_db(base, calibre_db)
        # except Exception as e:
        #     return self._handleError(str(e))
        #     pass

        # if everything is successfull, set the download tasks to Success
        for ui in self.UIqueue:
            if ui.get("id", None) in task_ids:
                ui['stat'] = STAT_FINISH_SUCCESS

        self._handleSuccess()


    def format_test(self):
        new_format = db.Data(name='Black Women in Science - Kimberly Brown Pellum, PhD',
                             book_format='AZW3',
                             book=1,
                             uncompressed_size=2664193)
        task = {'task': 'add_format', 'id': 1, 'format': new_format}
        self.db_queue.put(task)


    def _convert_any_format(self):
        # convert book, and upload in case of google drive
        self.doLock.acquire()
        index = self.current
        self.doLock.release()
        self.UIqueue[index]['stat'] = STAT_STARTED
        self.queue[index]['starttime'] = datetime.now()
        self.UIqueue[index]['formStarttime'] = self.queue[index]['starttime']
        curr_task = self.queue[index]['taskType']
        filename = self._convert_ebook_format()
        if filename:
            if config.config_use_google_drive:
                gdriveutils.updateGdriveCalibreFromLocal()
            if curr_task == TASK_CONVERT:
                self.add_email(self.queue[index]['settings']['subject'], self.queue[index]['path'],
                                filename, self.queue[index]['settings'], self.queue[index]['kindle'],
                                self.UIqueue[index]['user'], self.queue[index]['title'],
                                self.queue[index]['settings']['body'])

    def _convert_ebook_format(self):
        error_message = None
        self.doLock.acquire()
        index = self.current
        self.doLock.release()
        file_path = self.queue[index]['file_path']
        book_id = self.queue[index]['bookid']
        format_old_ext = u'.' + self.queue[index]['settings']['old_book_format'].lower()
        format_new_ext = u'.' + self.queue[index]['settings']['new_book_format'].lower()

        # check to see if destination format already exists -
        # if it does - mark the conversion task as complete and return a success
        # this will allow send to kindle workflow to continue to work
        if os.path.isfile(file_path + format_new_ext):
            log.info("Book id %d already converted to %s", book_id, format_new_ext)
            cur_book = calibre_db.get_book(book_id)
            self.queue[index]['path'] = file_path
            self.queue[index]['title'] = cur_book.title
            self._handleSuccess()
            return file_path + format_new_ext
        else:
            log.info("Book id %d - target format of %s does not exist. Moving forward with convert.",
                     book_id,
                     format_new_ext)

        if config.config_kepubifypath and format_old_ext == '.epub' and format_new_ext == '.kepub':
            check, error_message = self._convert_kepubify(file_path,
                                                          format_old_ext,
                                                          format_new_ext,
                                                          index)
        else:
            # check if calibre converter-executable is existing
            if not os.path.exists(config.config_converterpath):
                # ToDo Text is not translated
                self._handleError(_(u"Calibre ebook-convert %(tool)s not found", tool=config.config_converterpath))
                return
            check, error_message = self._convert_calibre(file_path, format_old_ext, format_new_ext, index)

        if check == 0:
            cur_book = calibre_db.get_book(book_id)
            if os.path.isfile(file_path + format_new_ext):
                # self.db_queue.join()
                new_format = db.Data(name=cur_book.data[0].name,
                                         book_format=self.queue[index]['settings']['new_book_format'].upper(),
                                         book=book_id, uncompressed_size=os.path.getsize(file_path + format_new_ext))
                task = {'task':'add_format','id': book_id, 'format': new_format}
                self.db_queue.put(task)
                # To Do how to handle error?

                '''cur_book.data.append(new_format)
                try:
                    # db.session.merge(cur_book)
                    calibre_db.session.commit()
                except OperationalError as e:
                    calibre_db.session.rollback()
                    log.error("Database error: %s", e)
                    self._handleError(_(u"Database error: %(error)s.", error=e))
                    return'''

                self.queue[index]['path'] = cur_book.path
                self.queue[index]['title'] = cur_book.title
                if config.config_use_google_drive:
                    os.remove(file_path + format_old_ext)
                self._handleSuccess()
                return file_path + format_new_ext
            else:
                error_message = format_new_ext.upper() + ' format not found on disk'
        log.info("ebook converter failed with error while converting book")
        if not error_message:
            error_message = 'Ebook converter failed with unknown error'
        self._handleError(error_message)
        return


    def _convert_calibre(self, file_path, format_old_ext, format_new_ext, index):
        try:
            # Linux py2.7 encode as list without quotes no empty element for parameters
            # linux py3.x no encode and as list without quotes no empty element for parameters
            # windows py2.7 encode as string with quotes empty element for parameters is okay
            # windows py 3.x no encode and as string with quotes empty element for parameters is okay
            # separate handling for windows and linux
            quotes = [1, 2]
            command = [config.config_converterpath, (file_path + format_old_ext),
                       (file_path + format_new_ext)]
            quotes_index = 3
            if config.config_calibre:
                parameters = config.config_calibre.split(" ")
                for param in parameters:
                    command.append(param)
                    quotes.append(quotes_index)
                    quotes_index += 1

            p = process_open(command, quotes)
        except OSError as e:
            return 1, _(u"Ebook-converter failed: %(error)s", error=e)

        while p.poll() is None:
            nextline = p.stdout.readline()
            if os.name == 'nt' and sys.version_info < (3, 0):
                nextline = nextline.decode('windows-1252')
            elif os.name == 'posix' and sys.version_info < (3, 0):
                nextline = nextline.decode('utf-8')
            log.debug(nextline.strip('\r\n'))
            # parse progress string from calibre-converter
            progress = re.search(r"(\d+)%\s.*", nextline)
            if progress:
                self.UIqueue[index]['progress'] = progress.group(1) + ' %'

        # process returncode
        check = p.returncode
        calibre_traceback = p.stderr.readlines()
        error_message = ""
        for ele in calibre_traceback:
            if sys.version_info < (3, 0):
                ele = ele.decode('utf-8')
            log.debug(ele.strip('\n'))
            if not ele.startswith('Traceback') and not ele.startswith('  File'):
                error_message = "Calibre failed with error: %s" % ele.strip('\n')
        return check, error_message


    def _convert_kepubify(self, file_path, format_old_ext, format_new_ext, index):
        quotes = [1, 3]
        command = [config.config_kepubifypath, (file_path + format_old_ext), '-o', os.path.dirname(file_path)]
        try:
            p = process_open(command, quotes)
        except OSError as e:
            return 1, _(u"Kepubify-converter failed: %(error)s", error=e)
        self.UIqueue[index]['progress'] = '1 %'
        while True:
            nextline = p.stdout.readlines()
            nextline = [x.strip('\n') for x in nextline if x != '\n']
            if sys.version_info < (3, 0):
                nextline = [x.decode('utf-8') for x in nextline]
            for line in nextline:
                log.debug(line)
            if p.poll() is not None:
                break

        # ToD Handle
        # process returncode
        check = p.returncode

        # move file
        if check == 0:
            converted_file = glob(os.path.join(os.path.dirname(file_path), "*.kepub.epub"))
            if len(converted_file) == 1:
                copyfile(converted_file[0], (file_path + format_new_ext))
                os.unlink(converted_file[0])
            else:
                return 1, _(u"Converted file not found or more than one file in folder %(folder)s",
                            folder=os.path.dirname(file_path))
        return check, None


    def add_convert(self, file_path, bookid, user_name, taskMessage, settings, kindle_mail=None):
        self.doLock.acquire()
        if self.last >= 20:
            self._delete_completed_tasks()
        # progress, runtime, and status = 0
        self.id += 1
        task = TASK_CONVERT_ANY
        if kindle_mail:
            task = TASK_CONVERT
        self.queue.append({'file_path':file_path, 'bookid':bookid, 'starttime': 0, 'kindle': kindle_mail,
                           'taskType': task, 'settings':settings})
        self.UIqueue.append({'user': user_name, 'formStarttime': '', 'progress': " 0 %", 'taskMess': taskMessage,
                             'runtime': '0 s', 'stat': STAT_WAITING,'id': self.id, 'taskType': task } )

        self.last=len(self.queue)
        self.doLock.release()


    def add_hb_download(self, user_name, bundle_name, product_name, download_info):
        self.doLock.acquire()
        # progress, runtime, and status = 0

        task_ids = []

        for x in download_info:
            # todo: this will have to add multiple tasks: one for each download. _But_ the first one will have to be a
            # "new" book, whereas the subsequent ones will have to be adding a format to it.
            self.id += 1

            self.queue.append({'taskType': TASK_HB_DOWNLOAD, 'download_info': x, "id": self.id})
            self.UIqueue.append({
                'user': user_name,
                'formStarttime': '',
                'progress': " 0 %",
                'taskMess': 'Downloading {book_title} ({format}) [{bundle_name}]'.format(
                    book_title=product_name, format=x["name"], bundle_name=bundle_name ),
                'runtime': '0 s',
                'stat': STAT_WAITING,
                'id': self.id,
                'taskType': TASK_HB_DOWNLOAD
            })
            task_ids.append(self.id)
            self.last=len(self.queue)

        # Here we create a special task that takes the download tasks and gathers the meta about them and links them under one book
        self.id += 1

        self.queue.append({
            'taskType': TASK_HB_LINK,
            'tasks': task_ids,
            "bundle_name": bundle_name,
            "product_name": product_name
        })

        self.UIqueue.append({
            'user': user_name,
            'formStarttime': '',
            'progress': " 0 %",
            'taskMess': 'Processing {book_title} [{bundle_name}]'.format(
                book_title=product_name, bundle_name=bundle_name),
            'runtime': '0 s',
            'stat': STAT_WAITING,
            'id': self.id,
            'taskType': TASK_HB_LINK
        })

        self.last = len(self.queue)

        self.doLock.release()


    def add_format_test(self, user_name):
        # if more than 20 entries in the list, clean the list
        self.doLock.acquire()
        if self.last >= 20:
            self._delete_completed_tasks()
        # progress, runtime, and status = 0
        self.id += 1
        self.queue.append({'starttime': 0, 'taskType': TASK_TEST})
        self.UIqueue.append({'user': user_name, 'formStarttime': '', 'progress': " 0 %", 'taskMess': "Testing format thing",
                             'runtime': '0 s', 'stat': STAT_WAITING,'id': self.id, 'taskType': TASK_TEST })
        self.last=len(self.queue)
        self.doLock.release()


    def add_email(self, subject, filepath, attachment, settings, recipient, user_name, taskMessage,
                  text):
        # if more than 20 entries in the list, clean the list
        self.doLock.acquire()
        if self.last >= 20:
            self._delete_completed_tasks()
        # progress, runtime, and status = 0
        self.id += 1
        self.queue.append({'subject':subject, 'attachment':attachment, 'filepath':filepath,
                           'settings':settings, 'recipent':recipient, 'starttime': 0,
                           'taskType': TASK_EMAIL, 'text':text})
        self.UIqueue.append({'user': user_name, 'formStarttime': '', 'progress': " 0 %", 'taskMess': taskMessage,
                             'runtime': '0 s', 'stat': STAT_WAITING,'id': self.id, 'taskType': TASK_EMAIL })
        self.last=len(self.queue)
        self.doLock.release()

    def add_upload(self, user_name, taskMessage):
        # if more than 20 entries in the list, clean the list
        self.doLock.acquire()


        if self.last >= 20:
            self._delete_completed_tasks()
        # progress=100%, runtime=0, and status finished
        self.id += 1
        starttime = datetime.now()
        self.queue.append({'starttime': starttime, 'taskType': TASK_UPLOAD})
        self.UIqueue.append({'user': user_name, 'formStarttime': starttime, 'progress': "100 %", 'taskMess': taskMessage,
                             'runtime': '0 s', 'stat': STAT_FINISH_SUCCESS,'id': self.id, 'taskType': TASK_UPLOAD})
        self.last=len(self.queue)
        self.doLock.release()

    def _send_raw_email(self):
        self.doLock.acquire()
        index = self.current
        self.doLock.release()
        self.queue[index]['starttime'] = datetime.now()
        self.UIqueue[index]['formStarttime'] = self.queue[index]['starttime']
        self.UIqueue[index]['stat'] = STAT_STARTED
        obj=self.queue[index]
        # create MIME message
        msg = MIMEMultipart()
        msg['Subject'] = self.queue[index]['subject']
        msg['Message-Id'] = make_msgid('calibre-web')
        msg['Date'] = formatdate(localtime=True)
        text = self.queue[index]['text']
        msg.attach(MIMEText(text.encode('UTF-8'), 'plain', 'UTF-8'))
        if obj['attachment']:
            result = get_attachment(obj['filepath'], obj['attachment'])
            if result:
                msg.attach(result)
            else:
                self._handleError(u"Attachment not found")
                return

        msg['From'] = obj['settings']["mail_from"]
        msg['To'] = obj['recipent']

        use_ssl = int(obj['settings'].get('mail_use_ssl', 0))
        try:
            # convert MIME message to string
            fp = StringIO()
            gen = Generator(fp, mangle_from_=False)
            gen.flatten(msg)
            msg = fp.getvalue()

            # send email
            timeout = 600  # set timeout to 5mins

            # redirect output to logfile on python2 pn python3 debugoutput is caught with overwritten
            # _print_debug function
            if sys.version_info < (3, 0):
                org_smtpstderr = smtplib.stderr
                smtplib.stderr = logger.StderrLogger('worker.smtp')

            if use_ssl == 2:
                self.asyncSMTP = email_SSL(obj['settings']["mail_server"], obj['settings']["mail_port"], timeout=timeout)
            else:
                self.asyncSMTP = email(obj['settings']["mail_server"], obj['settings']["mail_port"], timeout=timeout)

            # link to logginglevel
            if logger.is_debug_enabled():
                self.asyncSMTP.set_debuglevel(1)
            if use_ssl == 1:
                self.asyncSMTP.starttls()
            if obj['settings']["mail_password"]:
                self.asyncSMTP.login(str(obj['settings']["mail_login"]), str(obj['settings']["mail_password"]))
            self.asyncSMTP.sendmail(obj['settings']["mail_from"], obj['recipent'], msg)
            self.asyncSMTP.quit()
            self._handleSuccess()

            if sys.version_info < (3, 0):
                smtplib.stderr = org_smtpstderr

        except (MemoryError) as e:
            log.exception(e)
            self._handleError(u'MemoryError sending email: ' + str(e))
            return None
        except (smtplib.SMTPException, smtplib.SMTPAuthenticationError) as e:
            if hasattr(e, "smtp_error"):
                text = e.smtp_error.decode('utf-8').replace("\n",'. ')
            elif hasattr(e, "message"):
                text = e.message
            else:
                log.exception(e)
                text = ''
            self._handleError(u'Smtplib Error sending email: ' + text)
            return None
        except (socket.error) as e:
            self._handleError(u'Socket Error sending email: ' + e.strerror)
            return None

    def _handleError(self, error_message):
        log.error(error_message)
        self.doLock.acquire()
        index = self.current
        self.doLock.release()
        self.UIqueue[index]['stat'] = STAT_FAIL
        self.UIqueue[index]['progress'] = "100 %"
        self.UIqueue[index]['formRuntime'] = datetime.now() - self.queue[index]['starttime']
        self.UIqueue[index]['message'] = error_message

    def _handleSuccess(self):
        self.doLock.acquire()
        index = self.current
        self.doLock.release()
        self.UIqueue[index]['stat'] = STAT_FINISH_SUCCESS
        self.UIqueue[index]['progress'] = "100 %"
        self.UIqueue[index]['formRuntime'] = datetime.now() - self.queue[index]['starttime']


def get_taskstatus():
    return _worker.get_taskstatus()


def add_email(subject, filepath, attachment, settings, recipient, user_name, taskMessage, text):
    return _worker.add_email(subject, filepath, attachment, settings, recipient, user_name, taskMessage, text)


def add_upload(user_name, taskMessage):
    return _worker.add_upload(user_name, taskMessage)


def add_convert(file_path, bookid, user_name, taskMessage, settings, kindle_mail=None):
    return _worker.add_convert(file_path, bookid, user_name, taskMessage, settings, kindle_mail)


def add_hb_download(user_name, bundle_name, product_name, download_info):
    return _worker.add_hb_download(user_name, bundle_name, product_name, download_info)

def add_format_test(user_name):
    return _worker.add_format_test(user_name)

_worker = WorkerThread()
_worker.start()
