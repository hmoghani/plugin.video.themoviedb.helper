import re
import os
import json
import xbmcvfs
import unicodedata
from xbmcgui import Dialog
from resources.lib.addon.timedate import get_timedelta, get_datetime_now, is_future_timestamp
from resources.lib.addon.parser import try_int
from resources.lib.addon.plugin import ADDONDATA, kodi_log, get_localized, get_setting
from resources.lib.addon.constants import ALPHANUM_CHARS, INVALID_FILECHARS
try:
    import cPickle as _pickle
except ImportError:
    import pickle as _pickle  # Newer versions of Py3 just use pickle


def validify_filename(filename, alphanum=False):
    filename = unicodedata.normalize('NFD', filename)
    filename = u''.join([c for c in filename if (not alphanum or c in ALPHANUM_CHARS) and c not in INVALID_FILECHARS])
    return filename.strip('.')


def normalise_filesize(filesize):
    filesize = try_int(filesize)
    i_flt = 1024.0
    i_str = ['B', 'KB', 'MB', 'GB', 'TB']
    for i in i_str:
        if filesize < i_flt:
            return f'{filesize:.2f} {i}'
        filesize = filesize / i_flt
    return f'{filesize:.2f} PB'


def get_files_in_folder(folder, regex):
    return [x for x in xbmcvfs.listdir(folder)[1] if re.match(regex, x)]


def read_file(filepath):
    vfs_file = xbmcvfs.File(filepath)
    content = ''
    try:
        content = vfs_file.read()
    finally:
        vfs_file.close()
    return content


def get_tmdb_id_nfo(basedir, foldername, tmdb_type='tv'):
    try:
        folder = basedir + foldername + '/'

        # Get files ending with .nfo in folder
        nfo_list = get_files_in_folder(folder, regex=r".*\.nfo$")

        # Check our nfo files for TMDb ID
        for nfo in nfo_list:
            content = read_file(folder + nfo)  # Get contents of .nfo file
            tmdb_id = content.replace(f'https://www.themoviedb.org/{tmdb_type}/', '')  # Clean content to retrieve tmdb_id
            tmdb_id = tmdb_id.replace(u'&islocal=True', '')
            tmdb_id = try_int(tmdb_id)
            if tmdb_id:
                return f'{tmdb_id}'

    except Exception as exc:
        kodi_log(f'ERROR GETTING TMDBID FROM NFO:\n{exc}')


def get_file_path(folder, filename, join_addon_data=True):
    return os.path.join(get_write_path(folder, join_addon_data), filename)


def delete_file(folder, filename, join_addon_data=True):
    xbmcvfs.delete(get_file_path(folder, filename, join_addon_data))


def dumps_to_file(data, folder, filename, indent=2, join_addon_data=True):
    path = os.path.join(get_write_path(folder, join_addon_data), filename)
    with open(path, 'w') as file:
        json.dump(data, file, indent=indent)
    return path


def write_to_file(data, folder, filename, join_addon_data=True, append_to_file=False):
    path = '/'.join((get_write_path(folder, join_addon_data), filename))
    xbmcvfs.validatePath(xbmcvfs.translatePath(path))
    if append_to_file:
        data = '\n'.join([read_file(path), data])
    with xbmcvfs.File(path, 'w') as f:
        f.write(data)


def get_write_path(folder, join_addon_data=True):
    if join_addon_data:
        folder = f'{ADDONDATA}{folder}/'
    main_dir = xbmcvfs.validatePath(xbmcvfs.translatePath(folder))
    if not xbmcvfs.exists(main_dir):
        try:  # Try makedir to avoid race conditions
            xbmcvfs.mkdirs(main_dir)
        except FileExistsError:
            pass
    return main_dir


def _del_file(folder, filename):
    file = os.path.join(folder, filename)
    os.remove(file)


def del_old_files(folder, limit=1):
    folder = get_write_path(folder, True)
    for filename in sorted(os.listdir(folder))[:-limit]:
        _del_file(folder, filename)


def make_path(path, warn_dialog=False):
    if xbmcvfs.exists(path):
        return xbmcvfs.translatePath(path)
    if xbmcvfs.mkdirs(path):
        return xbmcvfs.translatePath(path)
    if get_setting('ignore_folderchecking'):
        kodi_log(f'Ignored xbmcvfs folder check error\n{path}', 2)
        return xbmcvfs.translatePath(path)
    kodi_log(f'XBMCVFS unable to create path:\n{path}', 2)
    if not warn_dialog:
        return
    Dialog().ok('XBMCVFS', f'{get_localized(32122)} [B]{path}[/B]\n{get_localized(32123)}')


def json_loads(obj):
    def json_int_keys(ordered_pairs):
        result = {}
        for key, value in ordered_pairs:
            try:
                key = int(key)
            except ValueError:
                pass
            result[key] = value
        return result
    return json.loads(obj, object_pairs_hook=json_int_keys)


def pickle_deepcopy(obj):
    return _pickle.loads(_pickle.dumps(obj))


def get_pickle_name(cache_name, alphanum=False):
    cache_name = cache_name or ''
    cache_name = cache_name.replace('\\', '_').replace('/', '_').replace('.', '_').replace('?', '_').replace('&', '_').replace('=', '_').replace('__', '_')
    return validify_filename(cache_name, alphanum=alphanum).rstrip('_')


def set_pickle(my_object, cache_name, cache_days=14, json_dump=False):
    if not my_object:
        return
    cache_name = get_pickle_name(cache_name)
    if not cache_name:
        return
    timestamp = get_datetime_now() + get_timedelta(days=cache_days)
    cache_obj = {'my_object': my_object, 'expires': timestamp.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(os.path.join(get_write_path('pickle'), cache_name), 'w' if json_dump else 'wb') as file:
        json.dump(cache_obj, file, indent=4) if json_dump else _pickle.dump(cache_obj, file)
    return my_object


def get_pickle(cache_name, json_dump=False):
    cache_name = get_pickle_name(cache_name)
    if not cache_name:
        return
    try:
        with open(os.path.join(get_write_path('pickle'), cache_name), 'r' if json_dump else 'rb') as file:
            cache_obj = json.load(file) if json_dump else _pickle.load(file)
    except IOError:
        cache_obj = None
    if cache_obj and is_future_timestamp(cache_obj.get('expires', '')):
        return cache_obj.get('my_object')


def use_pickle(func, *args, cache_name='', cache_only=False, cache_refresh=False, **kwargs):
    """
    Simplecache takes func with args and kwargs
    Returns the cached item if it exists otherwise does the function
    """
    my_object = get_pickle(cache_name) if not cache_refresh else None
    if my_object:
        return my_object
    elif not cache_only:
        my_object = func(*args, **kwargs)
        return set_pickle(my_object, cache_name)
