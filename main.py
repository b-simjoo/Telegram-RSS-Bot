import argparse
import html
import json
import logging
import os
import pickle
import random
import re
import string
import sys
import traceback
import typing
import BugReporter
import Handlers
from collections import OrderedDict
from configparser import ConfigParser
from datetime import datetime, timedelta
from threading import Timer
from urllib.request import urlopen
from urllib.error import HTTPError

import lmdb
from bs4 import BeautifulSoup
from dateutil.parser import parse
from telegram import (Chat, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto, ParseMode, ReplyKeyboardMarkup,
                      ReplyKeyboardRemove, Update, ChatMember)
from telegram.bot import Bot
from telegram.error import BadRequest, NetworkError, Unauthorized
from telegram.ext import (BaseFilter, CallbackContext, CallbackQueryHandler,
                          CommandHandler, ConversationHandler, Filters,
                          MessageHandler, ChatMemberHandler, Updater)
from telegram.utils.helpers import DEFAULT_NONE


import time
from functools import wraps


def retry(tries=4, delay=3, backoff=2):
    """Retry calling the decorated function using an exponential backoff.

    http://www.saltycrane.com/blog/2009/11/trying-out-retry-decorator-python/
    original from: http://wiki.python.org/moin/PythonDecoratorLibrary#Retry

    :param ExceptionToCheck: the exception to check. may be a tuple of
        exceptions to check
    :type ExceptionToCheck: Exception or tuple
    :param tries: number of times to try (not retry) before giving up
    :type tries: int
    :param delay: initial delay between retries in seconds
    :type delay: int
    :param backoff: backoff multiplier e.g. value of 2 will double the delay
        each retry
    :type backoff: int
    :param logger: logger to use. If None, print
    :type logger: logging.Logger instance
    """
    def deco_retry(f):

        @wraps(f)
        def f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay
            while mtries > 1:
                try:
                    return f(*args, **kwargs)
                except Exception as e:
                    msg = "%s, Retrying in %d seconds..." % (str(e), mdelay)
                    logging.warning(msg)
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            return f(*args, **kwargs)

        return f_retry  # true decorator

    return deco_retry


class BotHandler:

    #All supported tags by telegram seprated by '|'
    # this program will handle images it self
    SUPPORTED_HTML_TAGS = '|'.join(('a','b','strong','i','em','code','pre','s','strike','del','u'))
    SUPPORTED_TAG_ATTRS = {'a':'href', 'img':'src', 'pre':'language'}

    def __init__(
        self,
        Token,
        source,
        env,
        chats_db,
        data_db,
        strings: dict,
        bug_reporter = False,
        debug = False,
        request_kwargs=None):
        
        self.updater = Updater(Token, request_kwargs=request_kwargs)
        self.bot = self.updater.bot
        self.dispatcher = self.updater.dispatcher
        self.token = Token
        self.env = env
        self.chats_db = chats_db
        self.data_db = data_db
        self.adminID = self.get_data('adminID', [], DB = data_db)
        self.ownerID = self.get_data('ownerID', DB = data_db)
        self.admins_pendding = {}
        self.admin_token = []
        self.strings = strings
        self.source = source
        self.interval = self.get_data('interval', 5*60, data_db)
        self.__check__ = True
        self.bug_reporter = bug_reporter if bug_reporter else None
        self.debug = False

        if debug:
            Handlers.add_debuging_handlers(self)

        Handlers.add_users_handlers(self)
        Handlers.add_admin_handlers(self)
        Handlers.add_owner_handlers(self)
        Handlers.add_other_handlers(self)

    def log_bug(self, exc:Exception, msg='', report = True, disable_notification = False,**args):
        info = BugReporter.exception(msg, exc, report = self.bug_reporter and report)
        logging.exception(msg, exc_info=exc)
        msg = html.escape(msg)
        tb_string = html.escape(info['tb_string'])
        message = (
            '<b>An exception was raised</b>\n'
            '<i>L{line_no}@{file_name}: {exc_type}<i>\n'
            f'{msg}\n\n'
            f'<pre>{tb_string}</pre>'
        ).format_map(info)

        if len(args):
            message+='\n\nExtra info:'
            msg+='\n\nExtra info'
            for key, value in args.items():
                message+=f'\n<pre>{key} = {html.escape(json.dumps(value, indent = 2, ensure_ascii = False))}</pre>'
                msg+=f'\n{key} = {json.dumps(value, indent = 2, ensure_ascii = False)}'
        
        try:
            self.bot.send_message(chat_id = self.ownerID, text = message, parse_mode = ParseMode.HTML, disable_notification = disable_notification)
        except:
            logging.exception('can not send message to owner')

    def purge(self, html_str:str, images=True):
        tags = self.SUPPORTED_HTML_TAGS
        if images:
            tags+='|img'
        purge = re.compile(r'</?(?!(?:%s)\b)\w+[^>]*/?>'%tags).sub      #This regex will purge any unsupported tag
        soup = BeautifulSoup(purge('', html_str), 'html.parser')
        for tag in soup.descendants:
            #Remove any unsupported attribute
            if tag.name in self.SUPPORTED_TAG_ATTRS:
                attr = self.SUPPORTED_TAG_ATTRS[tag.name]
                if attr in tag.attrs:
                    tag.attrs = {attr: tag[attr]}
            else:
                tag.attrs = dict()
        return soup

    @retry(10)
    def get_feed(self):
        with urlopen(self.source) as f:
            return f.read().decode('utf-8')

    def read_feed(self):
        feeds_xml = None
        try:
            feeds_xml = self.get_feed()
        except Exception as e:
            self.log_bug(e,'exception while trying to get last feed', False, True)
            return None, None
        
        soup_page = BeautifulSoup(feeds_xml, 'xml')
        feeds_list = soup_page.findAll("item")
        skip = re.compile(r'</?[^>]*name = "skip"[^>]*>').match               #This regex will search for a tag named as "skip" like: <any name = "skip">
        for feed in feeds_list:
            try:
                description = str(feed.description.text)
                if not skip(description):     #if regex found something skip this post
                    soup = self.purge(description)
                    description = str(soup)
                    messages = [{'type': 'text', 'text': description, 'markup': None}]
                    #Handle images
                    images = soup.find_all('img')
                    first = True
                    if images:
                        for img in images:
                            sep = str(img)
                            have_link = img.parent.name == 'a'
                            if have_link:
                                sep = str(img.parent)
                            first_part, description = description.split(sep, 1)
                            if first:   #for first message
                                if first_part != '':
                                    messages[0] = {'type': 'text', 'text': first_part, 'markup': None}
                                    msg = {'type': 'image',
                                    'src': img['src'], 'markup': None}
                                    if have_link:
                                        msg['markup'] = [[InlineKeyboardButton('Open image link', img.parent['href'])]]
                                    messages.append(msg)
                                else:
                                    msg = {'type': 'image', 'src': img['src'], 'markup': None}
                                    if have_link:
                                        msg['markup'] = [[InlineKeyboardButton('Open image link', img.parent['href'])]]
                                    messages[0] = msg
                                first = False
                            else:
                                messages[-1]['text'] = first_part
                                msg = {'type': 'image',
                                    'src': img['src'], 'markup': None}
                                if have_link:
                                    msg['markup'] = [[InlineKeyboardButton('Open image link', img.parent['href'])]]
                                messages.append(msg)
                        #End for img
                        messages[-1]['text'] = description
                    #End if images
                    return feed, messages
            except Exception as e:
                self.log_bug(e,'Exception while reading feed', feed = str(feed))
                return None, None

    def send_feed(self, feed, messages, msg_header, chats):
        remove_ids = []
        if len(messages) != 0:
            try:
                if messages[-1]['markup']:
                    messages[-1]['markup'].append(
                        [InlineKeyboardButton('View post', str(feed.link.text))])
                else:
                    messages[-1]['markup'] = [[InlineKeyboardButton('View post', str(feed.link.text))]]
                
                msg_header = '<i>%s</i>\n\n<b><a href = "%s">%s</a></b>\n' % (
                    msg_header, feed.link.text, feed.title.text)
                messages[0]['text'] = msg_header+messages[0]['text']
                for chat_id, chat_data in chats:
                    for msg in messages:
                        try:
                            if msg['type'] == 'text':
                                self.bot.send_message(
                                    chat_id,
                                    msg['text'],
                                    parse_mode = ParseMode.HTML,
                                    reply_markup = InlineKeyboardMarkup(msg['markup']) if msg['markup'] else None
                                )
                            elif msg['type'] == 'image':
                                if msg['text'] == '':
                                    msg['text'] = None
                                self.bot.send_photo(
                                    chat_id,
                                    msg['src'],
                                    msg['text'],
                                    parse_mode = ParseMode.HTML,
                                    reply_markup = InlineKeyboardMarkup(msg['markup']) if msg['markup'] else None
                                )
                        except Unauthorized as e:
                            self.log_bug(e,'handled an exception while sending a feed to a user. removing chat', report=False, chat_id = chat_id, chat_data = chat_data)
                            try:
                                with self.env.begin(self.chats_db, write = True) as txn:
                                    txn.delete(str(chat_id).encode())
                            except Exception as e2:
                                self.log_bug(e2,'exception while trying to remove chat')
                                remove_ids.append(chat_id)
                        except Exception as e:
                            self.log_bug(e, 'Exception while sending a feed to a user', message = msg, chat_id = chat_id, chat_data = chat_data)
                            break

            except Exception as e:
                self.log_bug(e,'Exception while trying to send feed', messages = messages)

        for chat_id in remove_ids:
            with self.env.begin(self.chats_db, write = True) as txn:
                txn.delete(str(chat_id).encode())

    def iter_all_chats(self):
        with env.begin(self.chats_db) as txn:
            for key, value in txn.cursor():
                yield key.decode(), pickle.loads(value)

    def check_new_feed(self):
        feed, messages = self.read_feed()
        if feed:
            date = self.get_data('last-feed-date', DB = self.data_db)
            if date:
                feed_date = parse(feed.pubDate.text)
                if feed_date > date:
                    self.set_data('last-feed-date',
                                      feed_date, DB = self.data_db)
                    self.send_feed(feed, messages, self.get_string('new-feed'), self.iter_all_chats())
            else:
                feed_date = parse(feed.pubDate.text)
                self.set_data('last-feed-date',
                                  feed_date, DB = self.data_db)
                self.send_feed(feed, messages, self.get_string('new-feed'), self.iter_all_chats())
        if self.__check__:
            self.check_thread = Timer(self.interval, self.check_new_feed)
            self.check_thread.start()


    def get_data(self, key, default = None, DB = None, do = lambda data: pickle.loads(data)):
        DB = DB if DB else self.chats_db
        data = None
        with self.env.begin(DB) as txn:
            data = txn.get(key.encode(), default)
        if data is not default and callable(do):
            return do(data)
        else:
            return data

    def set_data(self, key, value, over_write = True, DB = None, do = lambda data: pickle.dumps(data)):
        DB = DB if DB else self.chats_db
        if not callable(do):
            do = lambda data: data
        with self.env.begin(DB, write = True) as txn:
            return txn.put(key.encode(), do(value), overwrite = over_write)

    def get_string(self, string_name):
        return ''.join(self.strings[string_name])

    def run(self):
        self.updater.start_polling()
        # check for new feed
        self.check_new_feed()

    def idle(self):
        self.updater.idle()
        self.updater.stop()
        self.__check__ = False
        self.check_thread.cancel()
        if self.check_thread.is_alive():
            self.check_thread.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser('main.py',
        description='Open source Telegram RSS-Bot server by bsimjoo\n'+\
            'https://github.com/bsimjoo/Telegram-RSS-Bot'
        )
    
    parser.add_argument('-r','--reset',
    help='Reset stored data about chats or bot data',
    default=False,required=False,choices=('data','chats','all'))

    parser.add_argument('-c','--config',
    help='Specify config file',
    default='user-config.conf', required=False, type=argparse.FileType('r'))

    args = parser.parse_args(sys.argv[1:])
    config = ConfigParser(allow_no_value=False)
    with args.config as cf:
        config.read_string(cf.read())
    main_config = config['main']
    file_name = main_config.get('log-file')
    logging.basicConfig(
        format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        filename=file_name,
        level = logging._nameToLevel.get(main_config.get('log-level','INFO').upper(),logging.INFO))
    env = lmdb.open(main_config.get('db-path','db.lmdb'), max_dbs = 3)
    chats_db = env.open_db(b'chats')
    data_db = env.open_db(b'config')        #using old name for compatibility

    if args.reset:
        answer = input(f'Are you sure you want to Reset all "{args.reset}"?(yes | anything else means no)')
        if answer != 'yes':
            exit()
        else:
            if args.reset in ('data', 'all'):
                with env.begin(data_db, write=True) as txn:
                    d=env.open_db()
                    txn.drop(d)
            if args.reset in ('chats', 'all'):
                with env.begin(chats_db, write=True) as txn:
                    d=env.open_db()
                    txn.drop(d)

    language = main_config.get('language','en-us')
    strings_file = main_config.get('strings-file', 'Default-strings.json')
    checks=[
        (strings_file, language),
        (strings_file, 'en-us'),
        ('Default-strings.json', language),
        ('Default-strings.json', 'en-us')
    ]
    strings = None
    for file, language in checks:
        if os.path.exists(file):
            with open(file) as f:
                strings = json.load(f)
            if language in strings:
                strings = strings[language]
                logging.info(f'using "{language}" language from "{file}" file')
                break
            else:
                logging.error(f'"{language}" language code not found in "{file}"')
        else:
            logging.error(f'file "{file}" not found')

    if not strings or strings == dict():
        logging.error('Cannot use a strings file. exiting...')
        exit(1)

    bug_reporter_mode = main_config.get('bug-reporter', 'off')
    if bug_reporter_mode in ('online', 'offline'):
        bugs_file = main_config.get('bugs-file','bugs.json')
        use_git = main_config.getboolean('use-git',False)
        git = main_config.get('git-command','git')
        git_source = main_config.get('git-source')
        BugReporter.quick_config(bugs_file, use_git, git, git_source)
        
        if bug_reporter_mode == 'online':
            try:
                from BugReporter import OnlineReporter
                import cherrypy
                
                conf = main_config.get('reporter-config-file','Bug-reporter.conf')
                if os.path.exists(conf):
                    cherrypy.log.access_log.propagate = False
                    cherrypy.tree.mount(OnlineReporter(),'/')
                    cherrypy.config.update(conf)
                    cherrypy.engine.start()
                
            except ModuleNotFoundError:
                logging.error('Cherrypy module not found, please first make sure that it is installed and then use http-bug-reporter')
                logging.info('Can not run http bug reporter, skipping http, saving bugs in bugs.json')
            except Exception as Argument:
                logging.exception("Error occurred while running http server")
                logging.info('Can not run http bug reporter, skipping http, saving bugs in bugs.json')
            else:
                logging.info(f'reporting bugs with http server and saving them as bugs.json')
        else:
            logging.info(f'saving bugs in bugs.json')

    token = main_config.get('token')
    if not token:
        logging.error("No Token, exiting")
        sys.exit()

    debug = main_config.getboolean('debug',fallback=False)

    use_proxy = main_config.getboolean('use-proxy', fallback=False)
    proxy_info = None
    if use_proxy:
        proxy_info={
            'proxy_url': main_config.get('proxy-url','socks5h://127.0.0.1:9090')
        }
        if 'proxy-user' in main_config:
            proxy_info['urllib3_proxy_kwargs'] = {
                'username': main_config.get('proxy-user'),
                'password': main_config.get('proxy-pass')
            }

    bot_handler = BotHandler(token, main_config.get('source','https://pcworms.blog.ir/rss/'), env,
                             chats_db, data_db, strings, not bug_reporter_mode == 'off', debug, proxy_info)
    bot_handler.run()
    bot_handler.idle()
    if bug_reporter_mode in ('online', 'offline'):
        logging.info('saving bugs report')
        BugReporter.dump()
    if bug_reporter_mode == 'online':
        logging.info('stoping http reporter')
        cherrypy.engine.stop()
    env.close()
    
