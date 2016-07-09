#!/usr/bin/env python

# This program is free software; you can redistribute it and/or modify
# it under the terms of version 2 (or later) of the GNU General Public
# License as published by the Free Software Foundation.

from __future__ import print_function

import audiotools
import logging
import multiprocessing
import os
import os.path
import plac
import re
import sys
import traceback

from audiotools import UnsupportedFile
from multiprocessing import Process
from multiprocessing.pool import ThreadPool
from mutagen import File as MusicFile
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4Tags
from subprocess import check_output

def tqdm_fake(iterable, *args, **kwargs):
    return iterable
try:
    from tqdm import tqdm as tqdm_real
except ImportError:
    # Fallback: No progress bars
    tqdm_real = tqdm_fake

# Set up logging
logFormatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.handlers = []
logger.addHandler(logging.StreamHandler())
for handler in logger.handlers:
    handler.setFormatter(logFormatter)

rg_tags = (
    "replaygain_track_gain",
    "replaygain_track_peak",
    "replaygain_album_gain",
    "replaygain_album_peak",
    "replaygain_reference_loudness",
)
for tag in rg_tags:
    # Support replaygain tags for MP3 and M4A/MP4
    id3_tagname = tag.upper()
    mp4_tagname = "----:com.apple.iTunes:" + tag
    EasyID3.RegisterTXXXKey(tag, id3_tagname)
    EasyMP4Tags.RegisterFreeformKey(tag, mp4_tagname)

def default_job_count():
    try:
        return multiprocessing.cpu_count()
    except Exception:
        return 1

def fullpath(f):
    """os.path.realpath + expanduser"""
    return os.path.realpath(os.path.expanduser(f))

def Property(function):
    keys = 'fget', 'fset', 'fdel'
    func_locals = {'doc':function.__doc__}
    def probe_func(frame, event, arg):
        if event == 'return':
            locals = frame.f_locals
            func_locals.update(dict((k,locals.get(k)) for k in keys))
            sys.settrace(None)
        return probe_func
    sys.settrace(probe_func)
    function()
    return property(**func_locals)

def get_multi(d, keys, default=None):
    '''Like "dict.get", but keys is a list of keys to try.

    The value for the first key present will be returned, or default
    if none of the keys are present.

    '''
    for k in keys:
        try:
            return d[k]
        except KeyError:
            pass
    return default

# Tag names copied from Quod Libet
def get_album(mf):
    return get_multi(mf, ("albumsort", "album"), [''])[0]
def get_albumartist(mf):
    return get_multi(mf, ("albumartistsort", "albumartist", "artistsort", "artist"), [''])[0]
def get_albumid(mf):
    return get_multi(mf, ("album_grouping_key", "labelid", "musicbrainz_albumid"), [''])[0]
def get_discnumber(mf):
    return mf.get("discnumber", [''])[0]
def get_full_classname(mf):
    t = type(mf)
    return "{}.{}".format(t.__module__, t.__qualname__)

class RGTrack(object):
    '''Represents a single track along with methods for analyzing it
    for replaygain information.'''

    def __init__(self, track):
        self.track = track

    def __repr__(self):
        return "RGTrack(MusicFile({}, easy=True))".format(repr(self.filename))

    def has_valid_rgdata(self):
        '''Returns True if the track has valid replay gain tags. The
        tags are not checked for accuracy, only existence.'''
        return self.gain and self.peak

    @Property
    def filename():
        def fget(self):
            return self.track.filename

    @Property
    def directory():
        def fget(self):
            return os.path.dirname(self.filename)

    @Property
    def track_set_key():
        def fget(self):
            return (self.directory,
                    get_full_classname(self.track),
                    get_album(self.track),
                    get_albumartist(self.track),
                    get_albumid(self.track),
                    get_discnumber(self.track))

    @Property
    def track_set_key_string():
        '''A human-readable string representation of the track_set_key.

        Unlike the key itself, this is not guaranteed to uniquely
        identify a track set.'''
        def fget(self):
            (dirname, classname, album, artist, albumid, disc) = self.track_set_key
            classname = re.sub("^.*\\.(Easy)?", "", classname)
            key_string = "{album}"
            if disc:
                key_string += " Disc {disc}"
            if artist:
                key_string += " by {artist}"
            key_string += " in directory {dirname} of type {ftype}"
            return key_string.format(
                album=album or "[No album]",
                disc=disc, artist=artist,
                dirname=dirname,
                ftype=classname)

    @Property
    def gain():
        doc = "Track gain value, or None if the track does not have replaygain tags."
        tag = 'replaygain_track_gain'
        def fget(self):
            try:
                return(self.track[tag])
            except KeyError:
                return None
        def fset(self, value):
            logger.debug("Setting %s to %s for %s" % (tag, value, self.filename))
            self.track[tag] = str(value)
        def fdel(self):
            if self.track.has_key(tag):
                del self.track[tag]

    @Property
    def peak():
        doc = "Track peak dB, or None if the track does not have replaygain tags."
        tag = 'replaygain_track_peak'
        def fget(self):
            try:
                return(self.track[tag])
            except KeyError:
                return None
        def fset(self, value):
            logger.debug("Setting %s to %s for %s" % (tag, value, self.filename))
            self.track[tag] = str(value)
        def fdel(self):
            if self.track.has_key(tag):
                del self.track[tag]

    @Property
    def length_seconds():
        def fget(self):
            return self.track.info.length

    def save(self):
        #print 'Saving "%s" in %s' % (os.path.basename(self.filename), os.path.dirname(self.filename))
        self.track.save()

class RGTrackDryRun(RGTrack):
    """Same as RGTrack, but the save() method does nothing.

    This means that the file will never be modified."""
    def save(self):
        pass

class RGTrackSet(object):
    '''Represents and album and supplies methods to analyze the tracks in that album for replaygain information, as well as store that information in the tracks.'''

    track_gain_signal_filenames = ('TRACKGAIN', '.TRACKGAIN', '_TRACKGAIN')

    def __init__(self, tracks, gain_type="auto"):
        self.RGTracks = { str(t.filename): t for t in tracks }
        if len(self.RGTracks) < 1:
            raise ValueError("Need at least one track to analyze")
        keys = set(t.track_set_key for t in self.RGTracks.values())
        if (len(keys) != 1):
            raise ValueError("All tracks in an album must have the same key")
        self.gain_type = gain_type

    def __repr__(self):
        return "RGTrackSet(%s, gain_type=%s)" % (repr(self.RGTracks.values()), repr(self.gain_type))

    @classmethod
    def MakeTrackSets(cls, tracks):
        '''Takes an unsorted list of RGTrack objects and returns a
        list of RGTrackSet objects, one for each track_set_key represented in
        the RGTrack objects.'''
        track_sets = {}
        for t in tracks:
            try:
                track_sets[t.track_set_key].append(t)
            except KeyError:
                track_sets[t.track_set_key] = [ t, ]
        return [ cls(track_sets[k]) for k in sorted(track_sets.keys()) ]

    def want_album_gain(self):
        '''Return true if this track set should have album gain tags,
        or false if not.'''
        if self.is_multitrack_album():
            if self.gain_type == "album":
                return True
            elif self.gain_type == "track":
                return False
            elif self.gain_type == "auto":
                # Check for track gain signal files
                return not any(os.path.exists(os.path.join(self.directory, f)) for f in self.track_gain_signal_filenames)
            else:
                raise TypeError('RGTrackSet.gain_type must be either "track", "album", or "auto"')
        else:
            # Single track(s), so no album gain
            return False

    @Property
    def gain():
        doc = "Album gain value, or None if tracks do not all agree on it."
        tag = 'replaygain_album_gain'
        def fget(self):
            return(self._get_tag(tag))
        def fset(self, value):
            self._set_tag(tag, value)
        def fdel(self):
            self._del_tag(tag)

    @Property
    def peak():
        doc = "Album peak value, or None if tracks do not all agree on it."
        tag = 'replaygain_album_peak'
        def fget(self):
            return(self._get_tag(tag))
        def fset(self, value):
            self._set_tag(tag, value)
        def fdel(self):
            self._del_tag(tag)

    @Property
    def filenames():
        def fget(self):
            return sorted(self.RGTracks.keys())

    @Property
    def num_tracks():
        def fget(self):
            return len(self.RGTracks)

    @Property
    def length_seconds():
        def fget(self):
            return sum(t.length_seconds for t in self.RGTracks.values())

    @Property
    def track_set_key():
        def fget(self):
            return next(iter(self.RGTracks.values())).track_set_key

    @Property
    def track_set_key_string():
        def fget(self):
            return next(iter(self.RGTracks.values())).track_set_key_string

    @Property
    def directory():
        def fget(self):
            return next(iter(self.RGTracks.values())).directory

    def _get_tag(self, tag):
        '''Get the value of a tag, only if all tracks in the album
        have the same value for that tag. If the tracks disagree on
        the value, return False. If any of the tracks is missing the
        value entirely, return None.

        In particular, note that None and False have different
        meanings.'''
        try:
            tags = set(tuple(t.track[tag]) for t in self.RGTracks.values())
            if len(tags) == 1:
                return tags.pop()
            elif len(tags) > 1:
                return False
            else:
                return None
        except KeyError:
            return None

    def _set_tag(self, tag, value):
        '''Set tag to value in all tracks in the album.'''
        logger.debug("Setting %s to %s in all tracks in %s.", tag, value, self.track_set_key_string)
        for t in self.RGTracks.values():
            t.track[tag] = str(value)

    def _del_tag(self, tag):
        '''Delete tag from all tracks in the album.'''
        logger.debug("Deleting %s in all tracks in %s.", tag, self.track_set_key_string)
        for t in self.RGTracks.values():
            try:
                del t.track[tag]
            except KeyError: pass

    def do_gain(self, force=False, gain_type=None, dry_run=False, verbose=False):
        """Analyze all tracks in the album, and add replay gain tags
        to the tracks based on the analysis.

        If force is False (the default) and the album already has
        replay gain tags, then do nothing.

        gain_type can be one of "album", "track", or "auto", as
        described in the help. If provided to this method, it will sef
        the object's gain_type field.
        """
        if self.has_valid_rgdata():
            if force:
                logger.info("Forcing reanalysis of previously-analyzed track set %s", repr(self.track_set_key_string))
            else:
                logger.info("Skipping previously-analyzed track set %s", repr(self.track_set_key_string))
                return
        else:
            logger.info('Analyzing track set %s', repr(self.track_set_key_string))
        audio_files = audiotools.open_files(self.filenames)
        if len(audio_files) != len(self.filenames):
            raise Exception("Could not load some files")
        rginfo = {}
        for rg in audiotools.calculate_replay_gain(audio_files):
            rginfo[rg[0].filename] = rg[1:3]
            # Store the album info with a key of None
            rginfo[None] = rg[3:5]
        # Now save the tags
        for fname in self.RGTracks.keys():
            track = self.RGTracks[fname]
            (track.gain, track.peak) = rginfo[fname]
        # Maybe save album gain
        if self.want_album_gain():
            (self.gain, self.peak) = rginfo[None]
        self.save()

    def is_multitrack_album(self):
        '''Returns True if this track set represents at least two
        songs, all from the same album. This will always be true
        unless except when one of the following holds:

        - the album consists of only one track;
        - the album is actually a collection of tracks that do not
          belong to any album.'''
        if len(self.RGTracks) <= 1 or self.track_set_key[0:1] is ('',''):
            return False
        else:
            return True

    def has_valid_rgdata(self):
        """Returns true if the album's replay gain data appears valid.
        This means that all tracks have replay gain data, and all
        tracks have the *same* album gain data (it want_album_gain is True).

        If the album has only one track, or if this album is actually
        a collection of albumless songs, then only track gain data is
        checked."""
        # Make sure every track has valid gain data
        for t in self.RGTracks.values():
            if not t.has_valid_rgdata():
                return False
        # For "real" albums, check the album gain data
        if self.want_album_gain():
            # These will only be non-null if all tracks agree on their
            # values. See _get_tag.
            if self.gain and self.peak:
                return True
            elif self.gain is None or self.peak is None:
                return False
            else:
                return False
        else:
            if self.gain is not None or self.peak is not None:
                return False
            else:
                return True

    def report(self):
        """Report calculated replay gain tags."""
        for k in sorted(self.filenames):
            track = self.RGTracks[k]
            logger.info("Set track gain tags for %s:\n\tTrack Gain: %s\n\tTrack Peak: %s", track.filename, track.gain, track.peak)
        if self.want_album_gain():
            logger.info("Set album gain tags for %s:\n\tAlbum Gain: %s\n\tAlbum Peak: %s", self.track_set_key_string, self.gain, self.peak)
        else:
            logger.info("Did not set album gain tags for %s.", self.track_set_key_string)

    def save(self):
        """Save the calculated replaygain tags"""
        self.report()
        for k in self.filenames:
            track = self.RGTracks[k]
            track.save()

def remove_hidden_paths(paths):
    '''Filter out UNIX-style hidden paths from an iterable.'''
    return ( p for p in paths if not re.search('^\.',p) )

def unique(items, key = None):
    '''Return an iterator over unique items, where two items are
    considered non-unique if "key(item)" returns the same value for
    both of them.

    If no key is provided, then the identity function is assumed by
    default.

    Note that this function caches the result of calling key() on
    every item in order to check for duplicates, so its memory usage
    is proportional to the length of the input.

    '''
    seen = set()
    for x in items:
        k = key(x) if key is not None else x
        if k in seen:
            pass
        else:
            yield x
            seen.add(k)

def is_music_file(file):
    # Exists?
    if not os.path.exists(file):
        logger.debug("File %s does not exist", repr(file))
        return False
    if not os.path.getsize(file) > 0:
        logger.debug("File %s has zero size", repr(file))
        return False
    # Readable by Mutagen?
    try:
        if not MusicFile(file):
            logger.debug("File %s is not recognized by Mutagen", repr(file))
            return False
    except Exception:
        logger.debug("File %s is not recognized", repr(file))
        return False
    # Readable by audiotools?
    try:
        audiotools.open(file)
    except UnsupportedFile:
        logger.debug("File %s is not recognized by audiotools", repr(file))
        return False
    # OK!
    return True

def get_all_music_files (paths, ignore_hidden=True):
    '''Recursively search in one or more paths for music files.

    By default, hidden files and directories are ignored.'''
    for p in paths:
        p = fullpath(p)
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p, followlinks=True):
                logger.debug("Searching for music files in %s", repr(root))
                if ignore_hidden:
                    # Modify dirs in place to cut off os.walk
                    dirs[:] = list(remove_hidden_paths(dirs))
                    files = remove_hidden_paths(files)
                files = filter(lambda f: is_music_file(os.path.join(root, f)), files)
                for f in files:
                    yield MusicFile(os.path.join(root, f), easy=True)
        else:
            logger.debug("Checking for music files at %s", repr(p))
            f = MusicFile(p, easy=True)
            if f is not None:
                yield f

class PickleableMethodCaller(object):
    """Pickleable method caller for multiprocessing.Pool.imap"""
    def __init__(self, method_name, *args, **kwargs):
        self.method_name = method_name
        self.args = args
        self.kwargs = kwargs
    def __call__(self, obj):
        try:
            return getattr(obj, self.method_name)(*self.args, **self.kwargs)
        except KeyboardInterrupt:
            sys.exit(1)

class TrackSetHandler(PickleableMethodCaller):
    """Pickleable callable for multiprocessing.Pool.imap"""
    def __init__(self, force=False, gain_type="auto", dry_run=False, verbose=False):
        super(TrackSetHandler, self).__init__(
            "do_gain",
            force = force,
            gain_type = gain_type,
            verbose = verbose,
            dry_run = dry_run,
        )
    def __call__(self, track_set):
        try:
            super(TrackSetHandler, self).__call__(track_set)
        except Exception:
            logger.error("Failed to analyze %s. Skipping this track set. The exception was:\n\n%s\n",
                         track_set.track_set_key_string, traceback.format_exc())
        return track_set