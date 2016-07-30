import os.path
import sys

from os import getenv
from shutil import which
from subprocess import Popen, PIPE, check_output, CalledProcessError

from rganalysis.common import logger
from rganalysis.backends import GainComputer, register_backend, BackendUnavailableException

try:
    from lxml import etree
except ImportError:
    raise BackendUnavailableException("Unable to use the bs1770gain backend: The lxml python module is not installed.")

bs1770gain_path = getenv("BS1770GAIN_PATH") or which("bs1770gain")
if not bs1770gain_path:
    raise BackendUnavailableException("Unable to use the bs1770gain backend: could not find bs1770gain executable in $PATH. To use this backend, ensure bs1770gain is in your $PATH or set BS1770GAIN_PATH environment variable to the path of the bs1770gain executable.")

class Bs1770gainGainComputer(GainComputer):
    def compute_gain(self, fnames, album=True):
        basenames_to_fnames = { os.path.basename(f): f for f in fnames }
        if len(basenames_to_fnames) != len(fnames):
            raise ValueError("The bs1770gain backend cannot handle multiple files with the same basename.")
        cmd = [bs1770gain_path, '--replaygain', '--integrated', '--samplepeak', '--xml', ] + fnames
        p = Popen(cmd, stdout=PIPE)
        tree = etree.parse(p.stdout)
        album = tree.xpath("/bs1770gain/album/summary")[0]
        album_gain = album.xpath("./integrated/@lu")[0]
        album_peak = album.xpath("./sample-peak/@factor")[0]
        tracks = tree.xpath("/bs1770gain/album/track")
        rginfo = {}
        for track in tracks:
            track_name = track.xpath("./@file")[0]
            track_gain = track.xpath("./integrated/@lu")[0]
            track_peak = track.xpath("./sample-peak/@factor")[0]
            rginfo[basenames_to_fnames[track_name]] = {
                "replaygain_track_gain": track_gain,
                "replaygain_track_peak": track_peak,
                "replaygain_album_gain": album_gain,
                "replaygain_album_peak": album_peak,
            }
        if p.wait() != 0:
            raise CalledProcessError(p.returncode, p.args)
        return rginfo

    def supports_file(self, fname):
        enc = sys.getdefaultencoding()
        p = Popen([bs1770gain_path, '-l', fname],
                  stderr=PIPE, stdout=PIPE)
        stdout, stderr = [ s.decode(enc) for s in p.communicate() ]
        if p.returncode != 0:
            return False
        if 'Input #' in stderr:
            return True
        else:
            return False

register_backend('bs1770gain', Bs1770gainGainComputer())