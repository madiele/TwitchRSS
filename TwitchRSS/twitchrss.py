#
# Copyright 2020 Laszlo Zeke
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from cachetools import cached, TTLCache, LRUCache
from feedformatter import Feed
from feedgen.feed import FeedGenerator
from flask import abort, Flask, request
from io import BytesIO
from os import environ
import datetime
import gzip
import json
import logging
import pytz
import re
import subprocess
import time
import urllib


VOD_URL_TEMPLATE = 'https://api.twitch.tv/helix/videos?user_id=%s&type=all'
USERID_URL_TEMPLATE = 'https://api.twitch.tv/helix/users?login=%s'
VODCACHE_LIFETIME = 10 * 60
USERIDCACHE_LIFETIME = 24 * 60 * 60
CHANNEL_FILTER = re.compile("^[a-zA-Z0-9_]{2,25}$")
TWITCH_CLIENT_ID = environ.get("TWITCH_CLIENT_ID")
TWITCH_SECRET = environ.get("TWITCH_SECRET")
TWITCH_OAUTH_TOKEN = ""
TWITCH_OAUTH_EXPIRE_EPOCH = 0
logging.basicConfig(level=logging.DEBUG if environ.get('DEBUG') else logging.INFO)

if not TWITCH_CLIENT_ID:
    raise Exception("Twitch API client id env variable is not set.")
if not TWITCH_SECRET:
    raise Exception("Twitch API secret env variable not set.")


app = Flask(__name__)

def authorize():
    global TWITCH_OAUTH_TOKEN
    global TWITCH_OAUTH_EXPIRE_EPOCH

    if (TWITCH_OAUTH_EXPIRE_EPOCH >= round(time.time())):
        return
    data = {
        'client_id': TWITCH_CLIENT_ID,
        'client_secret': TWITCH_SECRET,
        'grant_type': 'client_credentials',
    }
    url = 'https://id.twitch.tv/oauth2/token'
    request = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode("utf-8"), method='POST')
    retries = 0
    while retries < 3:
        try:
            result = urllib.request.urlopen(request, timeout=3)
            r = json.loads(result.read().decode("utf-8"))
            TWITCH_OAUTH_TOKEN = r['access_token']
            TWITCH_OAUTH_EXPIRE_EPOCH = int(r['expires_in']) + round(time.time())
            logging.info("oauth token aquired")
            return
        except Exception as e:
            logging.warning("Fetch exception caught: %s" % e)
            retries += 1
    abort(503)


@cached(cache=TTLCache(maxsize=3000, ttl=USERIDCACHE_LIFETIME))
def get_audiostream_url(vod_url):
    # sanitize the url from illegal characters
    vod_url = urllib.parse.quote(vod_url, safe='/:')
    command = ['streamlink', vod_url, "audio", "--stream-url"]
    p = subprocess.Popen(command, stdout=subprocess.PIPE, shell=False)
    out, err= p.communicate() 
    if p.returncode != 0:
        raise Exception("streamlink returned an error:" + out.decode() +"\n\n MAKE SURE YOU RUN THE LATEST VERSION OF STREAMLINK")
    return out.decode().rstrip("\n")

    
@app.route('/vod/<string:channel>', methods=['GET', 'HEAD'])
def vod(channel):
    if CHANNEL_FILTER.match(channel):
        return get_inner(channel)
    else:
        abort(404)


@app.route('/vodonly/<string:channel>', methods=['GET', 'HEAD'])
def vodonly(channel):
    if CHANNEL_FILTER.match(channel):
        return get_inner(channel, add_live=False)
    else:
        abort(404)


def get_inner(channel, add_live=True):
    user_json = fetch_userid(channel)
    if not user_json:
        abort(404)

    (channel_display_name, channel_id, icon) = extract_userid(json.loads(user_json)['data'][0])

    channel_json = fetch_vods(channel_id)
    if not channel_json:
        abort(404)

    decoded_json = json.loads(channel_json)['data']
    rss_data = construct_rss(channel, decoded_json, channel_display_name, icon, add_live)
    headers = {'Content-Type': 'text/xml'}

    if 'gzip' in request.headers.get("Accept-Encoding", ''):
        headers['Content-Encoding'] = 'gzip'
        rss_data = gzip.compress(rss_data)

    return rss_data, headers


@cached(cache=TTLCache(maxsize=3000, ttl=USERIDCACHE_LIFETIME))
def fetch_userid(channel_name):
    return fetch_json(channel_name, USERID_URL_TEMPLATE)


@cached(cache=TTLCache(maxsize=500, ttl=VODCACHE_LIFETIME))
def fetch_vods(channel_id):
    return fetch_json(channel_id, VOD_URL_TEMPLATE)


def fetch_json(id, url_template):
    authorize()
    url = url_template % id
    headers = {
        'Authorization': 'Bearer '+TWITCH_OAUTH_TOKEN,
        'Client-Id': TWITCH_CLIENT_ID,
        'Accept-Encoding': 'gzip'
    }
    request = urllib.request.Request(url, headers=headers)
    retries = 0
    while retries < 3:
        try:
            result = urllib.request.urlopen(request, timeout=3)
            logging.debug('Fetch from twitch for %s with code %s' % (id, result.getcode()))
            if result.info().get('Content-Encoding') == 'gzip':
                logging.debug('Fetched gzip content')
                return gzip.decompress(result.read())
            return result.read()
        except Exception as e:
            logging.warning("Fetch exception caught: %s" % e)
            retries += 1
    abort(503)


def extract_userid(user_info):
    # Get the first id in the list
    userid = user_info['id']
    username = user_info['display_name']
    icon = user_info['profile_image_url']
    if username and userid:
        return username, userid, icon
    else:
        logging.warning('Userid is not found in %s' % user_info)
        abort(404)


def construct_rss(channel_name, vods, display_name, icon, add_live=True):
    feed = FeedGenerator()

    # Set the feed/channel level properties
    feed.image(url=icon)
    feed.id("https://github.com/madiele/TwitchToPodcastRSS")
    feed.title("%s's Twitch video RSS" % display_name)
    feed.link(href='https://twitchrss.appspot.com/', rel='self')
    feed.author(name="Twitch RSS Generated")
    feed.description("The RSS Feed of %s's videos on Twitch" % display_name)

    # Create an item
    try:
        if vods:
            for vod in vods:
                logging.info("processing vod:")
                item = feed.add_entry()
                #if vod["status"] == "recording":
                #    if not add_live:
                #        continue
                #    link = "http://www.twitch.tv/%s" % channel_name
                #    item.title("%s - LIVE" % vod['title'])
                #    item.category("live")
                #else:
                link = vod['url']
                item.title(vod['title'])
                #item.category(vod['type'])
                item.enclosure(get_audiostream_url(link), type='audio/mpeg')
                item.link(href=link, rel="related")
                description = "<a href=\"%s\"><img src=\"%s\" /></a>" % (link, vod['thumbnail_url'].replace("%{width}", "512").replace("%{height}","288"))
                #if vod.get('game'):
                    #description += "<br/>" + vod['game']
                if vod['description']:
                    description += "<br/>" + vod['description']
                item.description(description)
                date = datetime.datetime.strptime(vod['created_at'], '%Y-%m-%dT%H:%M:%SZ')
                item.pubDate(pytz.utc.localize(date))
                item.updated(pytz.utc.localize(date))
                guid = vod['id']
                #if vod["status"] == "recording":  # To show a different news item when recording is over
                    #guid += "_live"
                item.guid(guid)
    except KeyError as e:
        logging.warning('Issue with json: %s\nException: %s' % (vods, e))
        abort(404)

    logging.info("all vods processed")
    return feed.atom_str()


# For debug
if __name__ == "__main__":
    app.run(host='127.0.0.1', port=8080, debug=True)
