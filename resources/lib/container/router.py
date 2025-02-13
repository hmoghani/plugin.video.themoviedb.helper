import sys
from xbmcplugin import addDirectoryItem, setProperty, setPluginCategory, setContent, endOfDirectory
from resources.lib.addon.constants import NO_LABEL_FORMATTING, RANDOMISED_TRAKT, RANDOMISED_LISTS, TRAKT_LIST_OF_LISTS, TMDB_BASIC_LISTS, TRAKT_BASIC_LISTS, TRAKT_SYNC_LISTS, ROUTE_NO_ID, ROUTE_TMDB_ID
from resources.lib.addon.plugin import convert_type, reconfigure_legacy_params, kodi_log, get_setting, executebuiltin
from resources.lib.addon.parser import parse_paramstring, try_int
from resources.lib.addon.setutils import split_items, random_from_list, merge_two_dicts
from resources.lib.addon.decorators import TimerList, ParallelThread
from resources.lib.api.mapping import set_show, get_empty_item, is_excluded
from resources.lib.api.kodi.rpc import get_kodi_library, get_movie_details, get_tvshow_details, get_episode_details, get_season_details, set_playprogress
from resources.lib.api.tmdb.api import TMDb
from resources.lib.api.tmdb.lists import TMDbLists
from resources.lib.api.tmdb.search import SearchLists
from resources.lib.api.tmdb.discover import UserDiscoverLists
from resources.lib.api.trakt.api import TraktAPI
from resources.lib.api.trakt.lists import TraktLists
from resources.lib.api.fanarttv.api import FanartTV
from resources.lib.api.omdb.api import OMDb
from resources.lib.items.builder import ItemBuilder
from resources.lib.items.basedir import BaseDirLists
from resources.lib.script.router import related_lists
from resources.lib.player.players import Players
# from resources.lib.files.utils import write_to_file
from threading import Thread


PREBUILD_PARENTSHOW = ['seasons', 'episodes', 'episode_groups', 'trakt_upnext', 'episode_group_seasons']
LOG_TIMER_ITEMS = ['item_api', 'item_tmdb', 'item_ftv', 'item_map', 'item_cache', 'item_set', 'item_get', 'item_getx', 'item_non', 'item_nonx', 'item_art']


class Container(TMDbLists, BaseDirLists, SearchLists, UserDiscoverLists, TraktLists):
    def __init__(self):
        # plugin:// params configuration
        self.handle = int(sys.argv[1])  # plugin:// handle
        self.paramstring = sys.argv[2][1:]  # plugin://plugin.video.themoviedb.helper?paramstring
        self.params = reconfigure_legacy_params(**parse_paramstring(self.paramstring))  # paramstring dictionary
        self.parent_params = self.params.copy()  # TODO: CLEANUP
        self.is_widget = self.params.pop('widget', '').lower() == 'true'
        self.is_cacheonly = self.params.pop('cacheonly', '').lower() == 'true'
        self.is_fanarttv = self.params.pop('fanarttv', '').lower()
        self.is_nextpage = self.params.pop('nextpage', '').lower() != 'false'
        self.filters = {
            'filter_key': self.params.get('filter_key', None),
            'filter_value': split_items(self.params.get('filter_value', None))[0],
            'exclude_key': self.params.get('exclude_key', None),
            'exclude_value': split_items(self.params.get('exclude_value', None))[0]
        }

        # endOfDirectory
        self.update_listing = False  # endOfDirectory(updateListing=) set True to replace current path
        self.plugin_category = ''  # Container.PluginCategory / ListItem.Property(widget)
        self.container_content = ''  # Container.Content({})
        self.container_update = ''  # Add path to call Containr.Update({}) at end of directory
        self.container_refresh = False  # True call Container.Refresh at end of directory
        self.library = None  # TODO: FIX -- Currently broken -- SetInfo(library, info)

        # KodiDB
        self.kodi_db = None
        self.kodi_db_tv = {}  # TODO: Move to KodiDB module

        # API class initialisation
        self.ib = None
        self.tmdb_api = TMDb(delay_write=True)
        self.trakt_api = TraktAPI(delay_write=True)
        self.omdb_api = OMDb(delay_write=True) if get_setting('omdb_apikey', 'str') else None
        self.ftv_api = FanartTV(cache_only=self.ftv_is_cache_only(), delay_write=True)

        # Log Settings
        self.log_timers = get_setting('timer_reports')
        self.timer_lists = {}

        # Trakt Watched Progress Settings
        self.trakt_hidewatched = get_setting('widgets_hidewatched') if self.is_widget else False
        self.trakt_watchedindicators = get_setting('trakt_watchedindicators')
        self.trakt_watchedinprogress = get_setting('trakt_watchedinprogress')
        self.trakt_playprogress = get_setting('trakt_playprogress')

        # Miscellaneous
        self.nodate_is_unaired = get_setting('nodate_is_unaired')  # Consider items with no date to be
        self.tmdb_cache_only = self.tmdb_is_cache_only()
        self.pagination = self.pagination_is_allowed()
        self.thumb_override = 0

    def pagination_is_allowed(self):
        if not self.is_nextpage:  # nextpage=false param overrides all other settings
            return False
        if self.is_widget and not get_setting('widgets_nextpage'):
            return False
        return True

    def ftv_is_cache_only(self):
        if self.is_cacheonly:  # cacheonly=true param overrides all other settings
            return True
        if self.is_fanarttv == 'true':
            return False
        if self.is_fanarttv == 'false':
            return True
        if self.is_widget and get_setting('widget_fanarttv_lookup'):  # user settings
            return False
        if not self.is_widget and get_setting('fanarttv_lookup'):  # user setting
            return False
        return True

    def tmdb_is_cache_only(self):
        if self.is_cacheonly:  # cacheonly=true param overrides all other settings
            return True
        if not self.ftv_is_cache_only():  # fanarttv lookups require TMDb lookups for tvshow ID -- TODO: only force on tvshows
            return False
        if get_setting('tmdb_details'):  # user setting
            return False
        return True

    def _add_item(self, i, pagination=True):
        if not pagination and 'next_page' in i:
            return
        with TimerList(self.timer_lists, 'item_api', log_threshold=0.05, logging=self.log_timers):
            return self.ib.get_listitem(i)

    def _make_item(self, li):
        if not li:
            return
        if not li.next_page and is_excluded(li, is_listitem=True, **self.filters):
            return

        # Reformat ListItem.Label for episodes to match Kodi default 1x01.Title
        # Check if unaired and either apply special formatting or hide item depending on user settings
        li.set_episode_label()
        if self.format_episode_labels and not li.infoproperties.get('specialseason'):
            if li.is_unaired(no_date=self.nodate_is_unaired):
                return

        # Add Trakt playcount and watched status
        li.set_details(details=self.get_kodi_details(li), reverse=True)  # Add details from Kodi library first if available
        li.set_playcount(playcount=self.get_playcount_from_trakt(li))
        if self.trakt_hidewatched and try_int(li.infolabels.get('playcount')) != 0:
            return

        li.set_context_menu()  # Set the context menu items
        li.set_uids_to_info()  # Add unique ids to properties so accessible in skins
        li.set_thumb_to_art(self.thumb_override == 2) if self.thumb_override else None  # Special override for calendars to prevent thumb spoilers
        li.set_params_reroute(self.is_fanarttv, self.params.get('extended'), self.is_cacheonly)  # Reroute details to proper end point
        li.set_params_to_info(self.plugin_category)  # Set path params to properties for use in skins
        li.infoproperties.update(self.property_params or {})
        if self.thumb_override:
            li.infolabels.pop('dbid', None)  # Need to pop the DBID if overriding thumb to prevent Kodi overwriting
        if li.next_page:
            li.params['plugin_category'] = self.plugin_category  # Carry the plugin category to next page in plugin:// path
        self.set_playprogress_from_trakt(li)
        return {'url': li.get_url(), 'listitem': li.get_listitem(), 'isFolder': li.is_folder}

    def add_items(self, items=None, pagination=True, property_params=None, kodi_db=None):
        if not items:
            return

        # Setup ItemBuilder
        self.ib = ItemBuilder(
            tmdb_api=self.tmdb_api, ftv_api=self.ftv_api, trakt_api=self.trakt_api,
            delay_write=True, cache_only=self.tmdb_cache_only)
        self.ib.timer_lists = self.ib._cache._timers = self.timer_lists
        self.ib.log_timers = self.log_timers

        # Prebuild parent show details
        if self.parent_params.get('info') in PREBUILD_PARENTSHOW:
            self.ib.get_parents(
                tmdb_type='tv', tmdb_id=self.parent_params.get('tmdb_id'),
                season=self.parent_params.get('season', None) if self.parent_params['info'] == 'episodes' else None)

        # Build items in threadss
        with TimerList(self.timer_lists, '--build', log_threshold=0.05, logging=self.log_timers):
            self.ib.parent_params = self.parent_params
            with ParallelThread(items, self._add_item, pagination) as pt:
                item_queue = pt.queue
            all_listitems = [i for i in item_queue if i]

        # Finalise listitems in parallel threads
        self._pre_sync.join()
        with TimerList(self.timer_lists, '--make', log_threshold=0.05, logging=self.log_timers):
            self.property_params = property_params
            self.format_episode_labels = self.parent_params.get('info') not in NO_LABEL_FORMATTING
            with ParallelThread(all_listitems, self._make_item) as pt:
                item_queue = pt.queue
            all_itemtuples = [i for i in item_queue if i]
            # Add items to directory
            for i in all_itemtuples:
                addDirectoryItem(handle=self.handle, **i)

    def set_params_to_container(self, **kwargs):
        params = {}
        for k, v in kwargs.items():
            if not k or not v:
                continue
            try:
                k = f'Param.{k}'
                v = f'{v}'
                params[k] = v
                setProperty(self.handle, k, v)  # Set params to container properties
            except Exception as exc:
                kodi_log(f'Error: {exc}\nUnable to set param {k} to {v}', 1)
        return params

    def finish_container(self, update_listing=False, plugin_category='', container_content=''):
        setPluginCategory(self.handle, plugin_category)  # Container.PluginCategory
        setContent(self.handle, container_content)  # Container.Content
        endOfDirectory(self.handle, updateListing=update_listing)

    def _set_playprogress_from_trakt(self, li):
        if li.infolabels.get('mediatype') == 'movie':
            return self.trakt_api.get_movie_playprogress(
                id_type='tmdb',
                unique_id=try_int(li.unique_ids.get('tmdb')))
        return self.trakt_api.get_episode_playprogress(
            id_type='tmdb',
            unique_id=try_int(li.unique_ids.get('tmdb')),
            season=li.infolabels.get('season'),
            episode=li.infolabels.get('episode'))

    def set_playprogress_from_trakt(self, li):
        if not self.trakt_playprogress:
            return
        if li.infolabels.get('mediatype') not in ['movie', 'episode']:
            return
        duration = li.infolabels.get('duration')
        if not duration:
            return
        progress = self._set_playprogress_from_trakt(li)
        if not progress:
            return
        if progress < 4 or progress > 96:
            return
        set_playprogress(li.get_url(), int(duration * progress // 100), duration)

    def pre_sync_trakt(self):
        list_info = self.params.get('info')
        tmdb_type = self.params.get('tmdb_type')

        info_movies = ['stars_in_movies', 'crew_in_movies', 'trakt_userlist']
        if tmdb_type in ['movie', 'both'] or list_info in info_movies:
            if self.trakt_watchedindicators:
                self.trakt_api.get_sync('watched', 'movie', 'tmdb')
            if self.trakt_playprogress:
                self.trakt_api.get_sync('playback', 'movie', 'tmdb')

        info_tvshow = ['stars_in_tvshows', 'crew_in_tvshows', 'trakt_userlist', 'trakt_calendar']
        if tmdb_type in ['tv', 'season', 'both'] or list_info in info_tvshow:
            tmdbid = try_int(self.params.get('tmdb_id'), fallback=None)
            season = try_int(self.params.get('season', -2), fallback=-2)  # Use -2 to force all seasons lookup data on Trakt at seasons level
            if self.trakt_watchedindicators:
                self.trakt_api.get_sync('watched', 'show', 'tmdb')
                if tmdbid:
                    self.trakt_api.get_episodes_airedcount(id_type='tmdb', unique_id=tmdbid, season=season)
                    self.trakt_api.get_episodes_watchcount(id_type='tmdb', unique_id=tmdbid, season=season)
            if self.trakt_playprogress and tmdbid and season != -2:
                self.trakt_api.get_sync('playback', 'show', 'tmdb')

    def get_playcount_from_trakt(self, li):
        if not self.trakt_watchedindicators:
            return
        if li.infolabels.get('mediatype') == 'movie':
            return self.trakt_api.get_movie_playcount(
                id_type='tmdb',
                unique_id=try_int(li.unique_ids.get('tmdb'))) or 0
        if li.infolabels.get('mediatype') == 'episode':
            return self.trakt_api.get_episode_playcount(
                id_type='tmdb',
                unique_id=try_int(li.unique_ids.get('tvshow.tmdb')),
                season=li.infolabels.get('season'),
                episode=li.infolabels.get('episode')) or 0
        if li.infolabels.get('mediatype') == 'tvshow':
            air_count = self.trakt_api.get_episodes_airedcount(
                id_type='tmdb',
                unique_id=try_int(li.unique_ids.get('tmdb')))
            if not air_count:
                return None if self.trakt_watchedinprogress else 0
            li.infolabels['episode'] = air_count
            return self.trakt_api.get_episodes_watchcount(
                id_type='tmdb',
                unique_id=try_int(li.unique_ids.get('tmdb'))) or 0
        if li.infolabels.get('mediatype') == 'season':
            air_count = self.trakt_api.get_episodes_airedcount(
                id_type='tmdb',
                unique_id=try_int(li.unique_ids.get('tmdb')),
                season=li.infolabels.get('season'))
            if not air_count:
                return None if self.trakt_watchedinprogress else 0
            li.infolabels['episode'] = air_count
            return self.trakt_api.get_episodes_watchcount(
                id_type='tmdb',
                unique_id=try_int(li.unique_ids.get('tmdb')),
                season=li.infolabels.get('season')) or 0

    def get_kodi_database(self, tmdb_type):
        with TimerList(self.timer_lists, ' - kodi_db', logging=self.log_timers):
            if get_setting('local_db'):
                return get_kodi_library(tmdb_type)

    def get_kodi_parent_dbid(self, li):
        if not self.kodi_db:
            return
        if li.infolabels.get('mediatype') in ['movie', 'tvshow']:
            return self.kodi_db.get_info(
                info='dbid',
                imdb_id=li.unique_ids.get('imdb'),
                tmdb_id=li.unique_ids.get('tmdb'),
                tvdb_id=li.unique_ids.get('tvdb'),
                originaltitle=li.infolabels.get('originaltitle'),
                title=li.infolabels.get('title'),
                year=li.infolabels.get('year'))
        if li.infolabels.get('mediatype') in ['season', 'episode']:
            return self.kodi_db.get_info(
                info='dbid',
                imdb_id=li.unique_ids.get('tvshow.imdb'),
                tmdb_id=li.unique_ids.get('tvshow.tmdb'),
                tvdb_id=li.unique_ids.get('tvshow.tvdb'),
                title=li.infolabels.get('tvshowtitle'))

    def get_kodi_details(self, li):
        if not self.kodi_db:
            return
        dbid = self.get_kodi_parent_dbid(li)
        if not dbid:
            return
        if li.infolabels.get('mediatype') == 'movie':
            return get_movie_details(dbid)
        if li.infolabels.get('mediatype') == 'tvshow':
            return get_tvshow_details(dbid)
        if li.infolabels.get('mediatype') == 'season':
            return set_show(self.get_kodi_tvchild_details(
                tvshowid=dbid,
                season=li.infolabels.get('season'),
                is_season=True) or get_empty_item(), get_tvshow_details(dbid))
        if li.infolabels.get('mediatype') == 'episode':
            return set_show(self.get_kodi_tvchild_details(
                tvshowid=dbid,
                season=li.infolabels.get('season'),
                episode=li.infolabels.get('episode')) or get_empty_item(), get_tvshow_details(dbid))

    def get_kodi_tvchild_details(self, tvshowid, season=None, episode=None, is_season=False):
        if not tvshowid or not season or (not episode and not is_season):
            return
        library = 'season' if is_season else 'episode'
        self.kodi_db_tv[tvshowid] = self.kodi_db_tv.get(tvshowid) or get_kodi_library(library, tvshowid)
        if not self.kodi_db_tv[tvshowid].database:
            return
        dbid = self.kodi_db_tv[tvshowid].get_info('dbid', season=season, episode=episode)
        if not dbid:
            return
        details = get_season_details(dbid) if is_season else get_episode_details(dbid)
        details['infoproperties']['tvshow.dbid'] = tvshowid
        return details

    def get_container_content(self, tmdb_type, season=None, episode=None):
        if tmdb_type == 'tv' and season and episode:
            return convert_type('episode', 'container')
        elif tmdb_type == 'tv' and season:
            return convert_type('season', 'container')
        return convert_type(tmdb_type, 'container')

    def list_randomised_trakt(self, **kwargs):
        kwargs['info'] = RANDOMISED_TRAKT.get(kwargs.get('info'), {}).get('info')
        kwargs['randomise'] = True
        self.parent_params = kwargs
        return self.get_items(**kwargs)

    def list_randomised(self, **kwargs):
        params = merge_two_dicts(kwargs, RANDOMISED_LISTS.get(kwargs.get('info'), {}).get('params'))
        item = random_from_list(self.get_items(**params))
        if not item:
            return
        self.plugin_category = f'{item.get("label")}'
        self.parent_params = item.get('params', {})
        return self.get_items(**item.get('params', {}))

    def get_tmdb_id(self, info, **kwargs):
        if info == 'collection':
            kwargs['tmdb_type'] = 'collection'
        return self.tmdb_api.get_tmdb_id(**kwargs)

    def _noop(self):
        return None

    def _get_items(self, func, **kwargs):
        return func['lambda'](getattr(self, func['getattr']), **kwargs)

    def get_items(self, **kwargs):
        info = kwargs.get('info')

        # Check routes that don't require ID lookups first
        route = ROUTE_NO_ID
        route.update(TRAKT_LIST_OF_LISTS)
        route.update(RANDOMISED_LISTS)
        route.update(RANDOMISED_TRAKT)

        # Early exit if we have a route
        if info in route:
            return self._get_items(route[info]['route'], **kwargs)

        # Check routes that require ID lookups second
        route = ROUTE_TMDB_ID
        route.update(TMDB_BASIC_LISTS)
        route.update(TRAKT_BASIC_LISTS)
        route.update(TRAKT_SYNC_LISTS)

        # Early exit to basedir if no route found
        if info not in route:
            return self.list_basedir(info)

        # Lookup up our TMDb ID
        if not kwargs.get('tmdb_id'):
            self.parent_params['tmdb_id'] = self.params['tmdb_id'] = kwargs['tmdb_id'] = self.get_tmdb_id(**kwargs)

        return self._get_items(route[info]['route'], **kwargs)

    def log_timer_report(self):
        total_log = self.timer_lists.pop('total', 0)
        timer_log = ['DIRECTORY TIMER REPORT\n', self.paramstring, '\n']
        timer_log.append('------------------------------\n')
        for k, v in self.timer_lists.items():
            if k in LOG_TIMER_ITEMS:
                avg_time = f'{sum(v) / len(v):7.3f} sec avg | {max(v):7.3f} sec max | {len(v):3}' if v else '  None'
                timer_log.append(f' - {k:12s}: {avg_time}\n')
            elif k[:4] == 'item':
                avg_time = f'{sum(v) / len(v):7.3f} sec avg | {sum(v):7.3f} sec all | {len(v):3}' if v else '  None'
                timer_log.append(f' - {k:12s}: {avg_time}\n')
            else:
                tot_time = f'{sum(v) / len(v):7.3f} sec' if v else '  None'
                timer_log.append(f'{k:15s}: {tot_time}\n')
        timer_log.append('------------------------------\n')
        tot_time = f'{sum(total_log) / len(total_log):7.3f} sec' if total_log else '  None'
        timer_log.append(f'{"Total":15s}: {tot_time}\n')
        for k, v in self.timer_lists.items():
            if v and k in LOG_TIMER_ITEMS:
                timer_log.append(f'\n{k}:\n{" ".join([f"{i:.3f} " for i in v])}\n')
        kodi_log(timer_log, 1)

    def get_directory(self):
        with TimerList(self.timer_lists, 'total', logging=self.log_timers):
            self._pre_sync = Thread(target=self.pre_sync_trakt)
            self._pre_sync.start()
            with TimerList(self.timer_lists, 'get_list', logging=self.log_timers):
                items = self.get_items(**self.params)
            if not items:
                return
            self.plugin_category = self.params.get('plugin_category') or self.plugin_category
            with TimerList(self.timer_lists, 'add_items', logging=self.log_timers):
                self.add_items(
                    items,
                    pagination=self.pagination,
                    property_params=self.set_params_to_container(**self.params),
                    kodi_db=self.kodi_db)
            self.finish_container(
                update_listing=self.update_listing,
                plugin_category=self.plugin_category,
                container_content=self.container_content)
        if self.log_timers:
            self.log_timer_report()
        if self.container_update:
            executebuiltin(f'Container.Update({self.container_update})')
        if self.container_refresh:
            executebuiltin('Container.Refresh')

    def play_external(self, **kwargs):
        kodi_log(['lib.container.router - Attempting to play item\n', kwargs], 1)
        if not kwargs.get('tmdb_id'):
            kwargs['tmdb_id'] = self.tmdb_api.get_tmdb_id(**kwargs)
        Players(**kwargs).play(handle=self.handle if self.handle != -1 else None)

    def context_related(self, **kwargs):
        if not kwargs.get('tmdb_id'):
            kwargs['tmdb_id'] = self.tmdb_api.get_tmdb_id(**kwargs)
        kwargs['container_update'] = True
        related_lists(include_play=True, **kwargs)

    def router(self):
        if self.params.get('info') == 'play':
            return self.play_external(**self.params)
        if self.params.get('info') == 'related':
            return self.context_related(**self.params)
        self.get_directory()
