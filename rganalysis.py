#!/usr/bin/env python
# Author: Ryan Thompson

# The Analysis class is modified from code found elsewhere. See the
# notice attached to that class. The Property function was found
# somewhere on the internet. The rest of the code is mine. Since the
# Analysis class is GPL2, then so is this file.

# This program is free software; you can redistribute it and/or modify
# it under the terms of version 2 (or later) of the GNU General Public
# License as published by the Free Software Foundation.

import plac
import logging

import sys
import os
import re
from os.path import realpath
import math
import traceback
import signal

# You must initialize the quodlibet config before tag editing will
# work correctly
import quodlibet.config
quodlibet.config.init()

from quodlibet.formats import MusicFile

from itertools import imap
import multiprocessing
from multiprocessing.pool import Pool

from subprocess import check_output

# http://stackoverflow.com/a/22434262/125921
from contextlib import contextmanager
# Set up logging
logFormatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.handlers = []
logger.addHandler(logging.StreamHandler())
for handler in logger.handlers:
    handler.setFormatter(logFormatter)

def fileno(file_or_fd):
    fd = getattr(file_or_fd, 'fileno', lambda: file_or_fd)()
    if not isinstance(fd, int):
        raise ValueError("Expected a file (`.fileno()`) or a file descriptor")
    return fd

@contextmanager
def stdout_redirected(to=os.devnull, stdout=None):
    if stdout is None:
       stdout = sys.stdout

    stdout_fd = fileno(stdout)
    # copy stdout_fd before it is overwritten
    #NOTE: `copied` is inheritable on Windows when duplicating a standard stream
    with os.fdopen(os.dup(stdout_fd), 'wb') as copied:
        stdout.flush()  # flush library buffers that dup2 knows nothing about
        try:
            os.dup2(fileno(to), stdout_fd)  # $ exec >&to
        except ValueError:  # filename
            with open(to, 'wb') as to_file:
                os.dup2(to_file.fileno(), stdout_fd)  # $ exec > to
        try:
            yield stdout # allow code to be run with the redirected stdout
        finally:
            # restore stdout to its previous value
            #NOTE: dup2 makes stdout_fd inheritable unconditionally
            stdout.flush()
            os.dup2(copied.fileno(), stdout_fd)  # $ exec >&copied

def default_job_count():
    try:
        return multiprocessing.cpu_count()
    except Exception:
        return 1

def decode_filename(f):
    if isinstance(f, str):
        f = f.decode(sys.getfilesystemencoding())
    return f

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

class RGTrack(object):
    '''Represents a single track along with methods for analyzing it
    for replaygain information.'''

    _track_set_key_functions = (lambda x: x.album_key,
                                lambda x: os.path.dirname(decode_filename(x['~filename'])),
                                lambda x: type(x),)

    def __init__(self, track):
        self.track = track
        self.track_set_key = tuple(f(self.track) for f in self._track_set_key_functions)

    def __repr__(self):
        return "RGTrack(%s)" % (repr(self.track), )

    def has_valid_rgdata(self):
        '''Returns True if the track has valid replay gain tags. The
        tags are not checked for accuracy, only existence.'''
        return self.gain and self.peak

    @Property
    def filename():
        def fget(self):
            return decode_filename(self.track['~filename'])
        def fset(self, value):
            self.track['~filename'] = value

    @Property
    def track_set_key_string():
        '''A human-readable string representation of the track_set_key.

        Unlike the key itself, this is not guaranteed to uniquely
        identify a track set.'''
        def fget(self):
            album = self.track("albumsort", "")
            disc = self.track("discnumber", "")
            (directory, filetype) = self.track_set_key[1:]

            if album == '':
                key_string = "No album"
            else:
                key_string = album
            if disc == '':
                pass
            else:
                key_string += " Disc " + disc
            key_string += " in directory %s" % (directory,)
            key_string += " of type %s" % (re.sub("File$","",filetype.__name__),)
            return key_string

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
            return self.track['~#length']

    def __len__(self):
        return self.length_seconds

    def save(self):
        #print 'Saving "%s" in %s' % (os.path.basename(self.filename), os.path.dirname(self.filename))
        self.track.write()

class RGTrackDryRun(RGTrack):
    """Same as RGTrack, but the save() method does nothing.

    This means that the file will never be modified."""
    def save(self):
        pass

class RGTrackSet(object):
    '''Represents and album and supplies methods to analyze the tracks in that album for replaygain information, as well as store that information in the tracks.'''

    track_gain_signal_filenames = ('TRACKGAIN', '.TRACKGAIN', '_TRACKGAIN')

    def __init__(self, tracks, gain_type="auto"):
        self.RGTracks = dict((t.filename, t) for t in tracks)
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
            return sum(t.length_seconds for t in self.RGTracks.itervalues())

    @Property
    def track_set_key():
        def fget(self):
            return next(self.RGTracks.itervalues()).track_set_key

    @Property
    def track_set_key_string():
        def fget(self):
            return next(self.RGTracks.itervalues()).track_set_key_string

    @Property
    def directory():
        def fget(self):
            return self.track_set_key[1]

    def __len__(self):
        return self.length_seconds

    def _get_tag(self, tag):
        '''Get the value of a tag, only if all tracks in the album
        have the same value for that tag. If the tracks disagree on
        the value, return False. If any of the tracks is missing the
        value entirely, return None.

        In particular, note that None and False have different
        meanings.'''
        try:
            tags = set(t.track[tag] for t in self.RGTracks.itervalues())
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
        for t in self.RGTracks.itervalues():
            t.track[tag] = str(value)

    def _del_tag(self, tag):
        '''Delete tag from all tracks in the album.'''
        logger.debug("Deleting %s in all tracks in %s.", tag, self.track_set_key_string)
        for t in self.RGTracks.itervalues():
            try:
                del t.track[tag]
            except KeyError: pass

    def do_gain(self, force=False, gain_type=None, dry_run=False, verbose=False, replaygain_path="replaygain"):
        """Analyze all tracks in the album, and add replay gain tags
        to the tracks based on the analysis.

        If force is False (the default) and the album already has
        replay gain tags, then do nothing.

        gain_type can be one of "album", "track", or "auto", as
        described in the help. If provided to this method, it will sef
        the object's gain_type field.
        """
        if gain_type:
            self.gain_type = gain_type

        # Only want album gain for real albums, not single tracks
        logger.info('Analyzing track set "%s"', self.track_set_key_string)
        cmd = [replaygain_path]
        if force:
            cmd.append("--force")
        if dry_run:
            cmd.append("--dry-run")
        if not self.want_album_gain():
            cmd.append("--no-album")
        cmd.extend(self.filenames)
        logger.debug("Executing command: %s", repr(cmd))
        output = check_output(cmd)
        # Print the output all at once to minimize the chance of interleaving
        if verbose:
            print output

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
    '''Remove UNIX-style hidden paths from a list.'''
    return [ p for p in paths if not re.search('^\.',p)]

def unique (items, key_fun = None):
    '''Return an unique list of items, where two items are considered
    non-unique if key_fun returns the same value for both of them.

    If no key_fun is provided, then the identity function is assumed,
    in which case this is equivalent to list(set(items)).'''
    if key_fun is None:
        return(list(set(items)))
    else:
        return(dict((key_fun(i), i) for i in items).values())

def get_all_music_files (paths, ignore_hidden=True):
    '''Recursively search in one or more paths for music files.

    By default, hidden files and directories are ignored.'''
    music_files = []
    with stdout_redirected(os.devnull, sys.stderr):
        for p in paths:
            if os.path.isdir(p):
                for root, dirs, files in os.walk(p, followlinks=True):
                    if ignore_hidden:
                        files = remove_hidden_paths(files)
                        dirs = remove_hidden_paths(dirs)
                    # Try to load every file as an audio file, and filter the
                    # ones that aren't actually audio files
                    more_files = ( MusicFile(os.path.join(root, x)) for x in files )
                    music_files.extend( f for f in more_files if f is not None )
            else:
                f = MusicFile(p)
                if f is not None:
                    music_files.append(f)

    # Filter duplicate files and return
    return(unique(music_files, key_fun=lambda x: x['~filename']))

class TrackSetHandler(object):
    """Pickleable stateful callable for multiprocessing.Pool.imap"""
    def __init__(self, force=False, gain_type="auto", dry_run=False, verbose=False,
                 replaygain_path="replaygain"):
        self.force = force
        self.gain_type = gain_type
        self.dry_run = dry_run
        self.replaygain_path = replaygain_path
    def __call__(self, track_set):
        try:
            track_set.do_gain(
                force=self.force, gain_type=self.gain_type, dry_run=self.dry_run,
                verbose=False, replaygain_path=self.replaygain_path)
        except Exception:
            logger.error("Failed to analyze %s. Skipping this track set. The exception was:\n\n%s\n", track_set.track_set_key_string, traceback.format_exc())
        return track_set

def positive_int(x):
    i = int(x)
    if i < 1:
        raise ValueError()
    else:
        return i

@plac.annotations(
    # arg=(helptext, kind, abbrev, type, choices, metavar)
    force_reanalyze=('Reanalyze all files and recalculate replaygain values, even if the files already have valid replaygain tags. Normally, only files missing or inconsistent replaygain tags will be analyzed.', "flag", "f"),
    include_hidden=('Do not skip hidden files and directories.', "flag", "i"),
    gain_type=('Can be "album", "track", or "auto". If "track", only track gain values will be calculated, and album gain values will be erased. if "album", both track and album gain values will be calculated. If "auto", then "album" mode will be used except in directories that contain a file called "TRACKGAIN" or ".TRACKGAIN". In these directories, "track" mode will be used. The default setting is "auto".',
        "option", "g", str, ('album', 'track', 'auto')),
    dry_run=("Don't modify any files. Only analyze and report gain.",
        "flag", "n"),
    music_directories=("Directories in which to search for music files.", "positional"),
    jobs=("Number of albums to analyze in parallel. The default is the number of cores detected on your system.", "option", "j", positive_int),
    replaygain_path=("Path to replaygain program", "option", "r", str),
    quiet=("Do not print informational messages.", "flag", "q"),
    verbose=("Print debug messages that are probably only useful if something is going wrong.", "flag", "v"),
    )
def main(force_reanalyze=False, include_hidden=False,
         dry_run=False, gain_type='auto',
         jobs=default_job_count(),
         replaygain_path="replaygain",
         quiet=False, verbose=False,
         *music_directories
         ):
    """Add replaygain tags to your music files."""
    if quiet:
        logger.setLevel(logging.WARN)
    elif verbose:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    # Some pesky functions used below will catch KeyboardInterrupts
    # inappropriately, so install an alternate handler that bypasses
    # KeyboardInterrupt instead.
    def signal_handler(sig, frame):
        logger.error("Canceled.")
        os.kill(os.getpid(), signal.SIGTERM)
    original_handler = signal.signal(signal.SIGINT, signal_handler)

    track_class = RGTrack
    if dry_run:
        logger.warn('This script is running in "dry run" mode, so no files will actually be modified.')
        track_class = RGTrackDryRun
    if len(music_directories) == 0:
        logger.error("You did not specify any music directories or files. Exiting.")
        sys.exit(1)

    music_directories = map(realpath, music_directories)
    logger.info("Searching for music files in the following directories:\n%s", "\n".join(music_directories),)
    tracks = [ track_class(f) for f in get_all_music_files(music_directories, ignore_hidden=(not include_hidden)) ]

    # Filter out tracks for which we can't get the length
    for t in tracks[:]:
        try:
            len(t)
        except Exception:
            logger.error("Track %s appears to be invalid. Skipping.", t.filename)
            tracks.remove(t)

    if len(tracks) == 0:
        logger.error("Failed to find any tracks in the directories you specified. Exiting.")
        sys.exit(1)
    track_sets = RGTrackSet.MakeTrackSets(tracks)

    # Remove the earlier bypass of KeyboardInterrupt
    signal.signal(signal.SIGINT, original_handler)

    logger.info("Beginning analysis")

    handler = TrackSetHandler(force=force_reanalyze, gain_type=gain_type, dry_run=dry_run, verbose=verbose)

    # For display purposes, calculate how much granularity is required
    # to show visible progress at each update
    total_length = sum(len(ts) for ts in track_sets)
    min_step = min(len(ts) for ts in track_sets)
    places_past_decimal = max(0,int(math.ceil(-math.log10(min_step * 100.0 / total_length))))
    update_string = '%.' + str(places_past_decimal) + 'f%% done'

    pool = None
    try:
        if jobs == 1:
            # Sequential
            handled_track_sets = imap(handler, track_sets)
        else:
            # Parallel
            pool = Pool(jobs)
            handled_track_sets = pool.imap_unordered(handler,track_sets)
        processed_length = 0
        percent_done = 0
        for ts in handled_track_sets:
            processed_length = processed_length + len(ts)
            percent_done = 100.0 * processed_length / total_length
            logger.info(update_string, percent_done)
        logger.info("Analysis complete.")
    except KeyboardInterrupt:
        if pool is not None:
            logger.debug("Terminating process pool")
            pool.terminate()
            pool = None
        raise
    finally:
        if pool is not None:
            logger.debug("Closing transcode process pool")
            pool.close()
    if dry_run:
        logger.warn('This script ran in "dry run" mode, so no files were actually modified.')
    pass

# Entry point
def plac_call_main():
    try:
        return plac.call(main)
    except KeyboardInterrupt:
        logger.error("Canceled.")
        sys.exit(1)

if __name__=="__main__":
    plac_call_main()
