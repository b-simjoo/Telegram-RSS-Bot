import argparse
import html
import commentjson
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
from bs4 import BeautifulSoup as Soup
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
    MAX_MSG_LEN = 4096
    MAX_CAP_LEN = 1024

    def __init__(
        self,
        Token,
        feed_configs,
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
        #`source` now is a property of `feed_config`
        self.feed_configs = feed_configs
        self.source = feed_config['source']
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
        Handlers.add_unknown_handlers(self)

        # New configurations:
        # - feed-format: specify feed fromat like xml or ...
        # - feeds-list-selector: how to find list of all feeds
        # - title-selector: how to find title
        # - link-selector: how to get link of source
        # - content-selector: how to get content
        # - skip-condition: how to check skip condition
        #   - format: feed/{selector}, content/{selector}, title/{regex}, none

        self.__skip = lambda feed: False
        skip_condition = feed_configs.get('feed-skip-condition','none')
        if skip_condition != 'none':
            self.__skip_field, skip_condition = skip_condition.split('/')
            if self.__skip_field == 'feed':
                self.__skip = lambda feed: bool(feed.select(skip_condition))
            elif self.__skip_field == 'content':
                self.__skip = lambda content: bool(content.select(skip_condition))
            elif self.__skip_field == 'title':
                match = re.compile(skip_condition).match
                self.__skip = lambda title: bool(match(title))

    def log_bug(self, exc:Exception, msg='', report = True, disable_notification = False,**args):
        info = BugReporter.exception(msg, exc, report = self.bug_reporter and report)
        logging.exception(msg, exc_info=exc)
        msg = html.escape(msg)
        escaped_info = {k:html.escape(str(v)) for k,v in info.items()}
        message = (
            '<b>An exception was raised</b>\n'
            '<i>L{line_no}@{file_name}: {exc_type}</i>\n'
            f'{msg}\n\n'
            '<pre>{tb_string}</pre>'
        ).format_map(escaped_info)

        if args:
            message+='\n\nExtra info:'
            msg+='\n\nExtra info'
            for key, value in args.items():
                message+=f'\n<pre>{key} = {html.escape(commentjson.dumps(value, indent = 2, ensure_ascii = False))}</pre>'
                msg+=f'\n{key} = {commentjson.dumps(value, indent = 2, ensure_ascii = False)}'
        
        try:
            while message:
                self.bot.send_message(chat_id = self.ownerID, text = str(self.purge(message[:self.MAX_MSG_LEN])), parse_mode = ParseMode.HTML, disable_notification = disable_notification)
                message = message[self.MAX_MSG_LEN:]
        except:
            logging.exception('can not send message to owner. message:\n'+message)

    def __get_content (self, tag):
        return ''.join([str(c) for c in tag.contents])

    def purge(self, soup:Soup, images=True):
        tags = self.SUPPORTED_HTML_TAGS
        if images:
            tags+='|img'
        #Remove remove elements with selector
        for elem in soup.select(self.feed_configs['remove-elements']):
            elem.replace_with('')
        for tag in soup.descendants:
            #Remove any unsupported tag and attribute
            if not tag.name in self.SUPPORTED_HTML_TAGS:
                tag.replacewith(self.__get_content(tag))
                #skip checking attrib for this tag
                continue
            if tag.name in self.SUPPORTED_TAG_ATTRS:
                attr = self.SUPPORTED_TAG_ATTRS[tag.name]
                if attr in tag.attrs:
                    tag.attrs = {attr: tag[attr]}
            else:
                tag.attrs = dict()
        return str(soup)

    @retry(10)
    def get_feeds(self):
        with urlopen(self.feed_configs['source']) as f:
            return f.read().decode('utf-8')

    def summarize(self, soup:Soup, length, read_more):
        offset = len(read_more)
        len_ = len(str(soup))
        if len_>length:
            offset += len_ - length
            removed = 0
            for element in reversed(list(soup.descendants)):
                if (not element.name) and len(str(element))>offset-removed:
                    s = str(element)
                    wrap_index = s.rfind(' ',0 , offset-removed)
                    if wrap_index == -1:
                        element.replace_with(s[:-offset+removed])
                        removed = offset
                    else:
                        element.replace_with(s[:wrap_index])
                    removed = offset
                else:
                    element.replace_with('')
                    removed += len(str(element))
                if removed >= offset:
                    break
        soup.append(read_more)
        return str(soup)

    def read_last_feed(self):
        # in this version fead reader uses css selector to get feeds.
        # 
        # New configurations:
        # - parse: specify feed fromat like xml or ...
        # - feeds-selector: how to get all feeds
        # - title-selector: how to find title
        # - link-selector: how to get link of source
        # - content-selector: how to get content
        # - feed-skip-condition: how to check skip condition
        #   - format: feed/{selector}, content/{selector}, title/{regex}, none
        # - remove-elements-selector: skip any element that has this attribute

        feeds_page = None
        try:
            feeds_page = self.get_feeds()
        except Exception as e:
            self.log_bug(e,'exception while trying to get last feed', False, True)
            return None, None
        
        soup_page = Soup(feeds_page, self.feed_configs.get('feed-format', 'xml'))
        feeds_list = soup_page.select(self.feed_configs['feeds-selector'])
        for feed in feeds_list:
            try:
                if self.__skip_field == 'feed':
                    if self.__skip(feed):
                        continue    #skip this feed

                title_selector = self.feed_configs['title-selector']
                title = None
                if title_selector:
                    # title-selector could be None (null)
                    title = str(feed.select(self.feed_configs['title-selector'])[0].text)

                    if self.__skip_field == 'title':
                        if self.__skip(title):
                            continue
                
                content_selector = self.feed_configs['content-selector']
                messages = []
                if content_selector:
                    content = Soup(self.__get_content(feed.select(content_selector)[0]))
                    content = self.purge(content)
                    images = content.find_all('img')
                    first = True

                    if not images:
                        if len(content)>self.MAX_MSG_LEN:
                            content = self.summarize(content, self.MAX_MSG_LEN, self.get_string('read-more'))
                        messages = [{'type': 'text', 'text': content, 'markup': None}]
                    
                    for img in images:
                        have_link = None
                        split_by = str(img)
                        if img:
                            have_link = img.parent.name == 'a'
                            if have_link:
                                split_by = str(img.parent)
                                link = img.parent['href']
                            first_part, content = content.split(split_by, 1)
                        else:
                            first_part = content
                            content = ''

                        if first:
                            if first_part:
                                messages[0] = {
                                    'type': 'text',
                                    'text': first_part[:self.MAX_MSG_LEN],
                                    'markup': None
                                }
                                first_part = first_part[self.MAX_MSG_LEN:]
                                while first_part:
                                    messages.append({
                                        'type': 'text',
                                        'text': first_part[:self.MAX_MSG_LEN],
                                        'markup': None
                                    })
                                if content:
                                    msg = {
                                        'type': 'image',
                                        'src': img['src'],
                                        'text': '',
                                        'markup': None
                                    }
                                    if have_link:
                                        msg['markup'] = [[InlineKeyboardButton('Open image link', link)]]
                                    messages.append(msg)
                            else:
                                msg = {
                                    'type': 'image',
                                    'src': img['src'],
                                    'text':'',
                                    'markup': None
                                }
                                if have_link:
                                    msg['markup'] = [[InlineKeyboardButton('Open image link', link)]]
                                messages[0] = msg
                            first = False
                        else:
                            messages[-1]['text'] += first_part[:self.MAX_CAP_LEN]
                            msg = {
                                'type': 'image',
                                'src': img['src'],
                                'text': first_part[self.MAX_CAP_LEN:],
                                'markup': None
                            }
                            if have_link:
                                msg['markup'] = [[InlineKeyboardButton('Open image link', link)]]
                            messages.append(msg)
                    #End for img
                    messages[-1]['text'] += content
                return feed, messages
            except Exception as e:
                self.log_bug(e,'Exception while reading feed', feed = str(feed))
                return None, None

    def send_feed(self, feed, messages, msg_header, chats):
        #TODO:Handle long captions and message
        # Long messages cause raising exception. So I must define 1024 chars for
        # captions and 4096 chars limition for messages.
        # labels: bug
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
        feed, messages = self.read_last_feed()
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
    default='user-config.json', required=False, type=argparse.FileType('r'))

    args = parser.parse_args(sys.argv[1:])
    config = dict()
    with args.config as cf:
        config = commentjson.load(cf)

    token = config.get('token')
    if not token:
        logging.error("No Token, exiting")
        sys.exit()
    
    log_file_name = config.get('log-file')
    logging.basicConfig(
        format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        filename=log_file_name,
        level = logging._nameToLevel.get(config.get('log-level','INFO').upper(),logging.INFO))
    env = lmdb.open(config.get('db-path','db.lmdb'), max_dbs = 3)
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
            sys.exit()

    language = config.get('language','en-us')
    strings_file = config.get('strings-file', 'Default-strings.json')
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
                strings = commentjson.load(f)
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
        sys.exit(1)

    bug_reporter_config = config.get('bug-reporter','off')
    if bug_reporter_config != 'off' and isinstance(bug_reporter_config, dict):
        bugs_file = bug_reporter_config.get('bugs-file','bugs.json')
        use_git = bug_reporter_config.get('use-git',False)
        git = bug_reporter_config.get('git-command','git')
        git_source = bug_reporter_config.get('git-source')
        BugReporter.quick_config(bugs_file, use_git, git, git_source)
        
        if 'http-config' in bug_reporter_config:
            try:
                from BugReporter import OnlineReporter
                import cherrypy
                
                conf = config.get('http-config',{
                    'global':{
                        'server.socket_host': '0.0.0.0',
                        'server.socket_port': 7191,
                        'log.screen': False
                    }
                })
                cherrypy.log.access_log.propagate = False
                cherrypy.tree.mount(OnlineReporter(),'/', config=conf)
                cherrypy.config.update(conf)
                cherrypy.engine.start()
                
            except ModuleNotFoundError:
                logging.error('Cherrypy module not found, please first make sure that it is installed and then use http-bug-reporter')
                logging.info(f'Can not run http bug reporter, skipping http, saving bugs in {bugs_file}')
            except:
                logging.exception("Error occurred while running http server")
                logging.info(f'Can not run http bug reporter, skipping http, saving bugs in {bugs_file}')
            else:
                logging.info(f'reporting bugs with http server and saving them as {bugs_file}')
        else:
            logging.info(f'saving bugs in {bugs_file}')

    debug = config.get('debug',False)

    use_proxy = config.get('use-proxy', False)
    proxy_info = None
    if use_proxy:
        proxy_info = config.get('proxy-info')

    feed_configs = config['feed-configs']

    bot_handler = BotHandler(token, config.get('source','https://pcworms.blog.ir/rss/'), env,
                             chats_db, data_db, strings, bug_reporter_config != 'off', debug, proxy_info, feed_configs)
    bot_handler.run()
    bot_handler.idle()
    if bug_reporter_config != 'off':
        logging.info('saving bugs report')
        BugReporter.dump()
    if 'http-config' in bug_reporter_config:
        logging.info('stoping http reporter')
        cherrypy.engine.stop()
    env.close()
    
