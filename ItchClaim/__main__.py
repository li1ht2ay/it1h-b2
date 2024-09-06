# The MIT License (MIT)
#
# Copyright (c) 2022-2024 Péter Tombor.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
import signal
from time import sleep
from typing import List

import pycron
from fire import Fire

from . import DiskManager, __version__
from .ItchGame import ItchGame
from .ItchUser import ItchUser
from .web import generate_web

import json
import requests
from bs4 import BeautifulSoup
from pathlib import Path

# pylint: disable=missing-class-docstring
class ItchClaim:
    def __init__(self,
                version: bool = False,
                login: str = None,
                password: str = None,
                totp: str = None):
        """Automatically claim free games from itch.io"""
        if version:
            self.version()
        # Try loading username from environment variables if not provided as command line argument
        if login is None and os.getenv('ITCH_USERNAME') is not None:
            login = os.getenv('ITCH_USERNAME')
        if login is not None:
            self.login(login, password, totp)
            while self.user.username == None:
                sleep(1)
                self.login(login, password, totp)
            print(f'Logged in as {self.user.username}', flush=True)
        else:
            self.user = None

    def version(self):
        """Display the version of the script and exit"""
        print(__version__)
        exit(0)

    def refresh_sale_cache(self, games_dir: str = 'web/data/', sales: List[int] = None):
        """Refresh the cache about game sales
        Opens itch.io and downloads sales posted after the last saved one.

        Args:
            games_dir (str): Output directory
            sales: (List[int]): Only refresh the sales specified in this list"""
        resume = 1
        ItchGame.games_dir = games_dir
        os.makedirs(games_dir, exist_ok=True)

        if sales:
            print('--sales flag found - refreshing only select sale pages')
            for sale_id in sales:
                DiskManager.get_one_sale(sale_id)
            return

        try:
            with open(os.path.join(games_dir, 'resume_index.txt'), 'r', encoding='utf-8') as f:
                resume = int(f.read())
                print(f'Resuming sale downloads from {resume}')
        except FileNotFoundError:
            print('Resume index not found. Downloading sales from beginning')

        DiskManager.get_all_sales(resume)

        print('Updating games from sale lists, to catch updates of already known sales.', flush=True)

        for category in ['games', 'tools', 'game-assets', 'comics', 'books', 'physical-games',
                'soundtracks', 'game-mods', 'misc']:
            print(f'Collecting sales from {category} list', flush=True)
            DiskManager.get_all_sale_pages(category=category)

    def refresh_library(self):
        """Refresh the list of owned games of an account. This is used to skip claiming already
        owned games. Requires login."""
        if self.user is None:
            print('You must be logged in')
            return
        self.user.reload_owned_games()
        self.user.save_session()

    def claim(self, url: str = 'https://itchclaim.tmbpeter.com/api/active.json'):
        """Claim all unowned games. Requires login.
        Args:
            url (str): The URL to download the file from"""

        if self.user is None:
            print('You must be logged in')
            return
        if len(self.user.owned_games) == 0:
            print('User\'s library not found in cache. Downloading it now', flush=True)
            self.user.reload_owned_games()
            self.user.save_session()

        print(f'Downloading free games list from {url}', flush=True)
        games = DiskManager.download_from_remote_cache(url)

        print('Claiming games', flush=True)
        claimed_games = 0
        for game in games:
            for owned_url in [owned_game.url for owned_game in self.user.owned_games]:
                if owned_url == url:
                    continue

            if not self.user.owns_game(game):
                self.user.claim_game(game)
                self.user.save_session()
                claimed_games += 1
        if claimed_games == 0:
            print('No new games can be claimed.', flush=True)

    def schedule(self, cron: str, url: str = 'https://itchclaim.tmbpeter.com/api/active.json'):
        """Start an infinite process of the script that claims games at a given schedule.
        Args:
            cron (str): The cron schedule to claim games
                See crontab.guru for syntax
            url (str): The URL to download the file from"""
        print(f'Starting cron job with schedule {cron}')

        # Define the signal handler
        def signal_handler(signum, frame):
            print("Interrupt signal received. Exiting...")
            exit(0)

        # Register the signal handler
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Start the scheduler
        while True:
            if not pycron.is_now(cron):
                sleep(60)
                continue
            self.claim(url)
            sleep(60)
        

    def download_urls(self, game_url: int):
        """Get details about a game, including it's CDN download URLs.

        Args:
            game_url (int): The url of the requested game."""
        game: ItchGame = ItchGame.from_api(game_url)
        session = self.user.s if self.user is not None else None
        print(game.downloadable_files(session))

    def generate_web(self, web_dir: str = 'web'):
        """Generates files that can be served as a static website
        
        Args:
            web_dir (str): Output directory"""

        ItchGame.games_dir = os.path.join(web_dir, 'data')
        os.makedirs(os.path.join(web_dir, 'api'), exist_ok=True)
        os.makedirs(ItchGame.games_dir, exist_ok=True)

        games = DiskManager.load_all_games()
        generate_web(games, web_dir)

    def login(self,
                username: str = None,
                password: str = None,
                totp: str = None) -> ItchUser:
        """Load session from disk if exists, or use password otherwise.

        Args:
            username (str): The username or email address of the user
            password (str): The password of the user
            totp (str): The 2FA code of the user
                Either the 6 digit code, or the secret used to generate the code
        
        Returns:
            ItchUser: a logged in ItchUser instance
        """
        self.user = ItchUser(username)
        try:
            self.user.load_session()
            print(f'Session {username} loaded successfully', flush=True)
        except FileNotFoundError:
            # Try loading password from environment variables if not provided as command line argument
            if password is None:
                password = os.getenv('ITCH_PASSWORD')
            # Try loading TOTP from environment variables if not provided as command line argument
            if totp is None:
                totp = os.getenv('ITCH_TOTP')
            self.user.login(password, totp)



    def _login(self):
        if self.user is None:
            print('You must be logged in', flush=True)
            return

        if len(self.user.owned_games) == 0:
            print('User\'s library not found in cache. Downloading it now', flush=True)
            self.user.reload_owned_games()
            self.user.save_session()


        self.active_sales = set()  # hashed, faster lookup
        self.future_sales = set()
        self.owned_items = set()


        active_db = DiskManager.download_from_remote_cache('https://itchclaim.tmbpeter.com/api/active.json')
        future_db = DiskManager.download_from_remote_cache('https://itchclaim.tmbpeter.com/api/upcoming.json')


        for game in active_db:
            self.active_sales.add(game.url)

        for game in future_db:
            self.future_sales.add(game.url)

        for game_url in [owned_game.url for owned_game in self.user.owned_games]:
            self.owned_items.add(game_url)



    def _substr(self, str, idx0, pat1, pat2):
        idx1 = str.find(pat1, idx0)
        if idx1 == -1:
            return None, -1

        idx1 += len(pat1)
        idx2 = str.find(pat2, idx1)

        if idx2 == -1:
            return None, -1

        return str[idx1:idx2], idx1



    def _dump_log(self, filename: str, mylist):
        if len(mylist) == 0:
            return

        with open(filename, 'w') as myfile:
            for line in mylist:
                print(line, file=myfile)  # Python 3.x



    def _send_web(self, type: str, url: str, redirect = True, payload = None):
        timer = 25

        count = 0
        while True:
            sleep(25/1000)

            try:
                if type == 'get':
                    r = requests.get(url, data=payload, timeout=timer, allow_redirects=redirect)
                if type == 'post':
                    r = requests.post(url, data=payload, timeout=timer, allow_redirects=redirect)


                if type == 'user_get':
                    r = self.user.s.get(url, data=payload, timeout=timer, allow_redirects=redirect)
                if type == 'user_post':
                    r = self.user.s.post(url, data=payload, timeout=timer, allow_redirects=redirect)


                r.encoding = 'utf-8'

            except requests.RequestException as err:
                print(err, flush=True)
                # pass


            if r.status_code == 200:  # OK
                break
            if r.status_code == 301:  # Redirect
                break
            if r.status_code == 404:  # Not found
                break
            if r.status_code == 429:  # Too many requests
                sleep(50/1000)
                continue
            if r.status_code >= 500:  # Server error
                break

        return r



    def _claim_reward(self, game: ItchGame):
        self.valid_reward = False
        self.scrape_count += 1


        try:
            r = self._send_web('get', game.url + '/data.json')

            dat = json.loads(r.text)
            if 'rewards' not in dat:
                print(game.url + '  #', flush=True)
                self.ignore_list.add(game.url)
                return


            for item in dat['rewards']:
                # print(item, flush=True)

                idx = 0
                while item['price'][idx].isdigit() == False:
                    idx += 1

                if item['price'][idx:] != '0.00':
                    continue


                # print(game.url, flush=True)
                self.valid_reward = True

                if item['available'] != True:
                    continue


                r = self._send_web('user_post', game.url + '/download_url?csrf_token=' + self.user.csrf_token + '&reward_id=' + str(item['id']))

                download_url = json.loads(r.text)['url']
                r = self._send_web('user_get', download_url)

                soup = BeautifulSoup(r.text, 'html.parser')
                claim_box = soup.find('div', class_='claim_to_download_box warning_box')
                if claim_box == None:
                    raise Exception("No claim box") 

                claim_url = claim_box.find('form')['action']
                r = self._send_web('user_post', claim_url, True, {'csrf_token': self.user.csrf_token})
                if r.url == 'https://itch.io/':
                    raise Exception(r.text)

                self.user.owned_games.append(game)
                print(f"Successfully claimed {game.url}", flush=True)

                break


        except Exception as err:
            print('[_claim_reward] Failure while claiming ' + game.url + ' = ' + str(err), flush=True)


        if self.valid_reward == True:
            print(game.url, flush=True)
            self.active_list.add(game.url)
            self.active_checked.add(game.url)
        else:
            print(game.url + '  #', flush=True)
            self.ignore_list.add(game.url)



    def _claim_game(self, game: ItchGame):
        try:
            r = self._send_web('user_post', game.url + '/download_url?csrf_token=' + self.user.csrf_token)
            r.encoding = 'utf-8'
            resp = json.loads(r.text)


            if 'errors' in resp:
                if resp['errors'][0] in ('invalid game', 'invalid user'):
                    if game.check_redirect_url():
                        self._claim_game(game)
                        return
                raise Exception(resp['errors'][0])


            download_url = json.loads(r.text)['url']
            r = self._send_web('user_get', download_url)
            r.encoding = 'utf-8'


            if r.status_code != 200:
                raise Exception(r.status_code)

            if 'Nothing is available for download yet.' in r.text:
                raise Exception('Nothing is available for download yet.')


            # if 'jubblands' in download_url:
            #    print(r.text)


            soup = BeautifulSoup(r.text, 'html.parser')
            claim_box = soup.find('div', class_='claim_to_download_box warning_box')
            if claim_box == None:
                print(game.url, flush=True)  # Python 3.x
                with open('itch-miss.txt', 'a') as myfile:
                    print(game.url, file=myfile)  # Python 3.x
                return


            claim_url = claim_box.find('form')['action']
            r = self._send_web('user_post', claim_url, True, {'csrf_token': self.user.csrf_token})
            r.encoding = 'utf-8'
            if r.url == 'https://itch.io/':
                if 'promotion is no longer active' in r.text:
                    raise Exception('promotion is no longer active')
                else:
                    raise Exception(r.text)

            else:
                self.owned_games.append(game)
                print(f"Successfully claimed {game.url}", flush=True)

        except Exception as err:
            print(f"ERROR: Failed to claim {game.url} = " + str(err), flush=True)



    def _claim_free(self, url: str = 'https://itchclaim.tmbpeter.com/api/active.json'):
        """Claim all unowned games. Requires login.
        Args:
            url (str): The URL to download the file from"""

        print(f'Downloading free games list from {url}', flush=True)
        games = DiskManager.download_from_remote_cache(url)

        print('Claiming games', flush=True)
        for game in games:
            for owned_url in [owned_game.url for owned_game in self.user.owned_games]:
                if owned_url == url:
                    continue

            if not self.user.owns_game(game):
                self._claim_game(game)



    def _scrape_profile(self, url, alt = False):
        try:
            self.profile_list.add(url)

            if self.scrape_count >= self.scrape_limit:
                return


            # print(url, flush=True)

            if alt == True:
                url = (self._substr(url, 0, 'https://', '.itch.io'))[0]
                url = 'https://itch.io/profile/' + url
                self.profile_checked_alt.add(url)
            else:
                self.profile_checked.add(url)


            r = self._send_web('get', url)


            str_index = 0
            while True:
                if self.scrape_count >= self.scrape_limit:
                    break


                str1 = r.text.find('class="game_cell has_cover lazy_images"', str_index)
                if str1 == -1:
                    break
                str_index = str1+1


                game = ItchGame(-1)
                game.url = (self._substr(r.text, str1, 'href="', '"'))[0]


                new_author = (self._substr(game.url, 0, 'https://', '.itch.io'))[0]
                new_profile = 'https://' + new_author + '.itch.io'

                if new_profile not in self.profile_list:
                    self.profile_list.add(new_profile)


                if game.url in self.owned_items:
                    continue
                if game.url in self.active_list:
                    continue
                if game.url in self.ignore_list:
                    continue

                self._claim_reward(game)


        except Exception as err:
            print('[_scrape_profile] Failure while checking ' + url + ' = ' + str(err), flush=True)



    def scrape_sales(self, scrape_page = -1, scrape_limit = -1, scrape_step = 5000):
        """Claim all unowned games. Requires login.
        Args:
            url (str): The URL to download the file from"""

        self._login()


        if scrape_limit == -1:
            r = self._send_web('get', 'http://itchclaim.tmbpeter.com/data/resume_index.txt')
            scrape_limit = int(r.text)

        if scrape_page == -1:
            r = self._send_web('get', 'https://raw.githubusercontent.com/li1ht2ay-3es/it1h-c2ai3/scrape-sales-ci/scrape.txt')
            scrape_page = int(r.text)


        miss_log = []  # not for searching
        future_log = []
        sales_log = []
        sales_list = []


        try:
            r = self._send_web('get', 'https://raw.githubusercontent.com/li1ht2ay-3es/it1h-c2ai3/zz-sales-' + str(scrape_page) + '/sales-url.txt')

            if r.status_code == 200:
                for sales_url in r.text.splitlines():
                    sales_list.append(sales_url)

        except Exception as err:
            print('Failure reading ' + 'sales-url.txt' + ' = ' + str(err), flush=True)


        print(f'Scraping {scrape_page} ...', flush=True)


        page_count = 0

        if scrape_page == 0:  # sale 0 = n/a
            page_count = 1
            scrape_page = 1


        while page_count < scrape_step and scrape_page < scrape_limit:
            try:
                if page_count < len(sales_list):
                    url = sales_list[page_count]

                else:
                    url = f"https://itch.io/s/{scrape_page}"
                    r = self._send_web('get', url, False)

                    if 'Location' in r.headers:
                        url = r.headers['Location']

                    sales_list.append(url)

                page_count += 1
                scrape_page += 1


                r = self._send_web('get', url)

                if 'This sale ended' in r.text:
                    continue

                if '100%</strong> off' not in r.text:
                    continue

                sale_url = url
                print(sale_url, flush=True)


                future_sale = False
                if 'class="not_active_notification">Come back' in r.text:
                    print('Future sale', flush=True)
                    future_sale = True


                idx = 0
                debug_sale = 0
                debug_miss = 0

                while True:
                    idx = r.text.find('class="game_cell_data"', idx)
                    if idx == -1:
                        break
                    idx += 1


                    url = (self._substr(r.text, idx, 'href="', '"'))[0]
                    print(url, flush=True)


                    if url not in self.active_sales and url not in self.future_sales:
                        print('Missing sale ' + url, flush=True)

                        if debug_sale == 0:
                            debug_sale = 1
                            sales_log.append(sale_url)

                        sales_log.append(url)


                    if url not in self.owned_items:
                        if future_sale:
                            print('Must claim later ' + url, flush=True)

                            if debug_miss == 0:
                                debug_miss = 1
                                future_log.append(sale_url)

                            future_log.append(url)

                        else:
                            game: ItchGame = ItchGame.from_api(url)
                            self.user.claim_game(game)

                            if url not in self.owned_items:
                                print('Not claimable ' + url, flush=True)

                                if debug_miss == 0:
                                    debug_miss = 1
                                    miss_log.append(sale_url)

                                miss_log.append(url)

            except Exception as err:
                print('Failure while checking ' + url + ' = ' + str(err), flush=True)


        self._dump_log('itch-miss.txt', miss_log)
        self._dump_log('itch-future.txt', future_log)
        self._dump_log('itch-sales.txt', sales_log)

        self._dump_log('sales-url.txt', sales_list)



    def scrape_rewards(self):
        """Claim all unowned games. Requires login.
        Args:
            url (str): The URL to download the file from"""
            # https://www.google.com/search?q=%2B%22itch.io%22+%2B%22free+community+Copy%22
            # https://www.google.com/search?q=itch.io+%22community+copies%22


        self._login()


        self.ignore_list = set()  # faster hashing
        self.active_list = set()
        self.active_checked = set()
        self.profile_list = set()
        self.profile_checked = set()
        self.profile_checked_alt = set()

        self.valid_reward = False
        self.scrape_count = 0
        self.scrape_limit = 1000  # 500 = 4m, 1000 = 6m, [2000] = 13m, 2500 = 16m, 5000 ~ 32m



        myfile = open('ignore.txt', 'r')
        for game_url in myfile.read().splitlines():
            # print(game_url)
            self.ignore_list.add(game_url)



        myfile = open('active.txt', 'r')
        for game_url in myfile.read().splitlines():
            if game_url in self.owned_items:
                continue
            if game_url in self.ignore_list:
                continue

            self.active_list.add(game_url)



        print(f'Checking old profiles ...', flush=True)

        myfile = open('profiles.txt', 'r')
        for profile_url in myfile.read().splitlines():
            try:
                if profile_url not in self.profile_checked:
                    self._scrape_profile(profile_url, True)

            except Exception as err:
                print('Failure while checking ' + profile_url + ' = ' + str(err), flush=True)



        print(f'Checking active profiles ...', flush=True)

        active_list_old = set(self.active_list)
        for game_url in active_list_old:
            try:
                new_author = (self._substr(game_url, 0, 'https://', '.itch.io'))[0]
                new_profile = 'https://' + new_author + '.itch.io'


                if new_profile not in self.profile_checked:
                    self._scrape_profile(new_profile, True)

            except Exception as err:
                print('Failure while checking ' + profile_url + ' = ' + str(err), flush=True)



        print(f'Checking collections ...', flush=True)

        myfile = open('collections.txt', 'r')
        for page_url in myfile.read().splitlines():
            page = 1
            url = page_url + '?format=json'

            try:
                while True:
                    # print(url, flush=True)
                    r = self._send_web('get', url)
                    dat = json.loads(r.text)

                    if dat['num_items'] == 0:
                        break

                    str_index = 0
                    while True:
                        str1 = dat['content'].find('class="game_cell has_cover lazy_images"', str_index)
                        if str1 == -1:
                            break
                        str_index = str1+1

                        new_author = (self._substr(dat['content'], str1, 'href="https://', '.itch.io'))[0]
                        new_profile = 'https://' + new_author + '.itch.io'


                        if new_profile not in self.profile_checked:
                            self._scrape_profile(new_profile, True)

                    page += 1
                    url = page_url + '?format=json&page=' + str(page)


            except Exception as err:
                print('[scrape_rewards] Failure while checking ' + url + ' = ' + str(err), flush=True)



        print(f'Checking new profiles ...', flush=True)

        profile_list_old = set(self.profile_list)
        for profile_url in profile_list_old:
            try:
                if profile_url not in self.profile_checked:
                    self._scrape_profile(profile_url, True)

                if profile_url not in self.profile_checked_alt:
                    self._scrape_profile(profile_url, False)

            except Exception as err:
                print('Failure while checking ' + profile_url + ' = ' + str(err), flush=True)



        print(f'Checking ignored profiles ...', flush=True)

        profile_list_ignore = set(self.ignore_list)
        for profile_url in profile_list_ignore:
            try:
                if profile_url not in self.profile_checked:
                    self._scrape_profile(profile_url, True)

                if profile_url not in self.profile_checked_alt:
                    self._scrape_profile(profile_url, False)

            except Exception as err:
                print('Failure while checking ' + profile_url + ' = ' + str(err), flush=True)



        print(str(self.scrape_count) + ' / ' + str(self.scrape_limit))



        with open('active.txt', 'w') as myfile:
            for line in sorted(self.active_list):
                print(line, file=myfile)  # Python 3.x

        with open('ignore.txt', 'w') as myfile:
            for line in sorted(self.ignore_list):
                print(line, file=myfile)  # Python 3.x

        with open('profiles.txt', 'w') as myfile:
            for line in sorted(self.profile_list):
                print(line, file=myfile)  # Python 3.x



    def auto_rating(self):
        """Rate one game. Requires login.
        Args:
            url (str): Game to claim"""

        rated_games = []
        url = 'https://itch.io/library/rated?json'

        page_num = 1
        try:
            while True:
                # print(url, flush=True)

                r = self._send_web('user_get', url)
                dat = json.loads(r.text)

                if 'game_ratings' not in dat:
                    break

                extra = 0
                for item in dat['game_ratings']:
                    # print(item, flush=True)
                    rated_games.append(item['game']['id'])
                    extra += 1

                print('Reviews page #' + str(page_num) + ': added ' + str(extra) + ' games (total: ' + str(len(rated_games)) + ')', flush=True)

                if 'next_page' not in dat:
                    break

                next_url = str(dat['next_page']).replace("'", '"')
                url = 'https://itch.io/library/rated?json&next_page=' + next_url
                page_num += 1

        except Exception as err:
            print('Failure to get reviews ' + url + ' = ' + str(err), flush=True)


        for game in self.user.owned_games:
            url = game.url
            try:
                for rated in rated_games:
                    if game.id == rated:
                        url = None
                        break

                if url == None:
                    continue

                print('Rating ' + url, flush=True)

                data = {
                    'csrf_token': self.user.csrf_token,
                    'game_rating': '5'
                }

                r = self._send_web('user_post', url + '/rate?source=game&game_id=' + str(game.id), True, data)
                if 'errors' in r.text:
                    continue

                # print(r.text)
                print('Success!', flush=True)
                # return

            except Exception as err:
                print('Failure to rate ' + url + ' = ' + str(err), flush=True)



    def make_report(self):
        """Go through logs."""

        class _sale_item(object):
            def __init__(self):
                self.id = None
                self.list = []
                self.start = None
                self.end = None


        def _sale_add(list, item, order):
            idx = 0
            while idx < len(list):
                if order == True:
                    if item.start < list[idx].start:
                        break

                else:
                    if item.start > list[idx].start:
                        break

                idx += 1
            list.insert(idx, item)


        def _create_report(url, list, file, check, order):
            try:
                item = _sale_item()
                sale_url = None


                my_file = Path(url + '/' + file)
                if not my_file.exists():
                    return

                with open(my_file, 'r') as myfile:
                    for line in my_file.read_text().splitlines():
                        if line.find('itch.io/s/') != -1:
                            sale_url = line

                            if item.id != None:
                                _sale_add(list, item, order)


                            item = _sale_item()

                            r = self._send_web('get', line)
                            if r.status_code == 200:
                                item.start = (self._substr(r.text, 0, '"start_date":"', '"'))[0]
                                item.end = (self._substr(r.text, 0, '"end_date":"', '"'))[0]
                            continue


                        if check == True and line in self.owned_items:
                            continue


                        r = self._send_web('get', line)

                        if r.status_code != 200:
                            continue

                        if 'A password is required to view this page' in r.text:
                            continue

                        if '<p>This asset pack is currently unavailable</p>' in r.text:
                            continue

                        if '<p>This game is currently unavailable</p>' in r.text:
                            continue


                        if item.id == None:
                            sale_id = (self._substr(sale_url, 0, '/s/', '/'))[0]
                            item.id = 'https://itch.io/s/' + str(sale_id)
                            print(sale_url, flush=True)

                        print(line, flush=True)
                        item.list.append(line)


                    if item.id != None:
                        _sale_add(list, item, order)

            except Exception as err:
                print('Failed to check ' + url + '/' + file + ' = ' + str(err), flush=True)


        def _print_report(list, name):
            if len(list) == 0:
                return

            my_file = Path(name)

            with open(my_file, 'w') as myfile:
                for item in list:
                    print(f"{str(item.id):50s} {str(item.start):25s} {str(item.end):25s}", file=myfile)  # Python 3.x
                    for game in item.list:
                        print(game, file=myfile)  # Python 3.x



        self._login()

        r = self._send_web('get', 'https://itchclaim.tmbpeter.com/data/resume_index.txt')
        scrape_limit = int(r.text)


        with open('itch-owned.txt', 'w') as myfile:
            for game in self.user.owned_games:
                if game.url == None:
                    continue
                print(f'{game.name:60s} {game.url:50s}', file=myfile)  # Python 3.x


        active_list = []
        miss_list = []
        future_list = []

        # print(os.listdir())


        page = 0
        page -= 5000
        while page < scrape_limit:
            page += 5000
            url = 'it1h-c2ai3-zz-sales-' + str(page)

            _create_report(url, future_list, 'itch-future.txt', True, True)
            _create_report(url, miss_list, 'itch-miss.txt', True, False)
            _create_report(url, active_list, 'itch-sales.txt', False, False)


        _print_report(future_list, 'itch-future.txt')
        _print_report(miss_list, 'itch-miss.txt')
        _print_report(active_list, 'itch-sales.txt')



    def claim_url(self, url):
        """Claim one game. Requires login.
        Args:
            url (str): Game to claim"""

        self.scrape_count = 0

        print(f'Attempting to claim {url}', flush=True)
        game: ItchGame = ItchGame.from_api(url)
        self.user.claim_game(game)
        self._claim_reward(game)



    def claim_rewards(self):
        """Claim all unowned games. Requires login.
        Args:
            url (str): The URL to download the file from"""
            # https://www.google.com/search?q=%2B%22itch.io%22+%2B%22free+community+Copy%22
            # https://www.google.com/search?q=itch.io+%22community+copies%22


        self._login()

        self.scrape_count = 0

        self.active_list = set()
        self.active_checked = set()
        self.ignore_list = set()


        myfile = open('active.txt', 'r')
        for game_url in myfile.read().splitlines():
            if game_url in self.owned_items:
                continue

            game = ItchGame(-1)
            game.url = game_url

            self._claim_reward(game)



    def download_url(self, url):
        """Claim one game. Requires login.
        Args:
            url (str): Game to claim"""

        def _get_game(game: ItchGame):
            try:
                r = self._send_web('user_post', game.url + '/download_url?csrf_token=' + self.user.csrf_token)
                r.encoding = 'utf-8'
                resp = json.loads(r.text)


                if 'errors' in resp:
                    if resp['errors'][0] in ('invalid game', 'invalid user'):
                        if game.check_redirect_url():
                            _get_game(game)
                            return
                    raise Exception(resp['errors'][0])


                download_url = json.loads(r.text)['url']
                r = self._send_web('user_get', download_url)
                r.encoding = 'utf-8'


                if r.status_code != 200:
                    raise Exception(r.status_code)

                if 'Nothing is available for download yet.' in r.text:
                    raise Exception('Nothing is available for download yet.')


                with open('debug.htm', 'w', encoding="utf-8") as myfile:
                    print(r.text, file=myfile)


                downloadid = (self._substr(r.text, 0, 'data-upload_id="', '"'))[0]
                download_url = game.url + '/file/' + str(downloadid) + '?source=game_download&after_download_lightbox=1&as_props=1'
                print(str(downloadid))

                r = self._send_web('user_post', download_url)
                r.encoding = 'utf-8'

                with open('debug2.htm', 'w', encoding="utf-8") as myfile:
                    print(r.text, file=myfile)


                download_url = json.loads(r.text)['url']
                print(download_url)

                r = self.user.s.get(download_url, stream=True)
                r.raw.decode_content = True

                with open('debug3.bin', 'wb') as myfile:
                    for chunk in r.iter_content(chunk_size=1024): 
                        myfile.write(chunk)

            except Exception as err:
                print(f"ERROR: Failed to get {game.url} = " + str(err), flush=True)


        print(f'Attempting to download {url}', flush=True)
        game = ItchGame(-1)
        game.url = url
        _get_game(game)



    def sync(self):
        self._login()


        self.master_list = set()
        self.owned_items = set()

        with open('itch-master.txt', 'r') as myfile:
            for game_url in myfile.read().splitlines():
                self.master_list.add(game_url)

        for game_url in [owned_game.url for owned_game in self.user.owned_games]:
            self.master_list.add(game_url)
            self.owned_items.add(game_url)


        self.auto_rating()
        self._claim_free()


        with open('itch-owned.txt', 'w') as myfile:
            for game in self.user.owned_games:
                if game.url == None:
                    continue
                print(f'{game.name:60s} {game.url:50s}', file=myfile)  # Python 3.x


        with open('itch-master.txt', 'w') as myfile:
            for game_url in sorted(self.master_list):
                print(game_url, file=myfile)  # Python 3.x


        with open('itch-removed.txt', 'w') as myfile:
            for game_url in sorted(self.master_list):
                if game_url not in self.owned_items:
                    print(game_url, file=myfile)  # Python 3.x


#        with open('debug.htm', 'w', encoding="utf-8") as myfile:
#            print(r.text, file=myfile)


# pylint: disable=missing-function-docstring
def main():
    Fire(ItchClaim)
