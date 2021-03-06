#! /usr/bin/env python
'''
Tahoe thin-client fuse module.

See the accompanying README for configuration/usage details.

Goals:

- Delegate to Tahoe webapi as much as possible.
- Thin rather than clever.  (Even when that means clunky.)


Warts:

- Reads cache entire file contents, violating the thinness goal.  Can we GET spans of files?
- Single threaded.


Road-map:
1. Add unit tests where possible with little code modification.
2. Make unit tests pass for a variety of python-fuse module versions.
3. Modify the design to make possible unit test coverage of larger portions of code.

Wishlist:
- Perhaps integrate cli aliases or root_dir.cap.
- Research pkg_resources; see if it can replace the try-import-except-import-error pattern.
- Switch to logging instead of homebrew logging.
'''


#import bindann
#bindann.install_exception_handler()

import sys, stat, os, errno, urllib, time

try:
    import simplejson
except ImportError, e:
    raise SystemExit('''\
Could not import simplejson, which is bundled with Tahoe.  Please
update your PYTHONPATH environment variable to include the tahoe
"support/lib/python<VERSION>/site-packages" directory.

If you run this from the Tahoe source directory, use this command:
PYTHONPATH="$PYTHONPATH:./support/lib/python%d.%d/site-packages/" python %s
''' % (sys.version_info[:2] + (' '.join(sys.argv),)))
    

try:
    import fuse
except ImportError, e:
    raise SystemExit('''\
Could not import fuse, the pythonic fuse bindings.  This dependency
of tahoe-fuse.py is *not* bundled with tahoe.  Please install it.
On debian/ubuntu systems run: sudo apt-get install python-fuse
''')

# FIXME: Check for non-working fuse versions here.
# FIXME: Make this work for all common python-fuse versions.

# FIXME: Currently uses the old, silly path-based (non-stateful) interface:
fuse.fuse_python_api = (0, 1) # Use the silly path-based api for now.


### Config:
TahoeConfigDir = '~/.tahoe'
MagicDevNumber = 42
UnknownSize = -1


def main():
    basedir = os.path.expanduser(TahoeConfigDir)

    for i, arg in enumerate(sys.argv):
        if arg == '--basedir':
            try:
                basedir = sys.argv[i+1]
                sys.argv[i:i+2] = []
            except IndexError:
                sys.argv = [sys.argv[0], '--help']
                

    log_init(basedir)
    log('Commandline: %r', sys.argv)

    fs = TahoeFS(basedir)
    fs.main()


### Utilities for debug:
_logfile = None # Private to log* functions.

def log_init(confdir):
    global _logfile
    
    logpath = os.path.join(confdir, 'logs', 'tahoe_fuse.log')
    _logfile = open(logpath, 'a')
    log('Log opened at: %s\n', time.strftime('%Y-%m-%d %H:%M:%S'))


def log(msg, *args):
    _logfile.write((msg % args) + '\n')
    _logfile.flush()
    
    
def trace_calls(m):
    def dbmeth(self, *a, **kw):
        pid = self.GetContext()['pid']
        log('[%d %r]\n%s%r%r', pid, get_cmdline(pid), m.__name__, a, kw)
        try:
            r = m(self, *a, **kw)
            if (type(r) is int) and (r < 0):
                log('-> -%s\n', errno.errorcode[-r],)
            else:
                repstr = repr(r)[:256]
                log('-> %s\n', repstr)
            return r
        except:
            sys.excepthook(*sys.exc_info())
            
    return dbmeth


def get_cmdline(pid):
    f = open('/proc/%d/cmdline' % pid, 'r')
    args = f.read().split('\0')
    f.close()
    assert args[-1] == ''
    return args[:-1]


class SystemError (Exception):
    def __init__(self, eno):
        self.eno = eno
        Exception.__init__(self, errno.errorcode[eno])

    @staticmethod
    def wrap_returns(meth):
        def wrapper(*args, **kw):
            try:
                return meth(*args, **kw)
            except SystemError, e:
                return -e.eno
        wrapper.__name__ = meth.__name__
        return wrapper


### Heart of the Matter:
class TahoeFS (fuse.Fuse):
    def __init__(self, confdir):
        log('Initializing with confdir = %r', confdir)
        fuse.Fuse.__init__(self)
        self.confdir = confdir
        
        self.flags = 0 # FIXME: What goes here?
        self.multithreaded = 0

        # silly path-based file handles.
        self.filecontents = {} # {path -> contents}

        self._init_url()
        self._init_rootdir()

    def _init_url(self):
        if os.path.exists(os.path.join(self.confdir, 'node.url')):
            self.url = file(os.path.join(self.confdir, 'node.url'), 'rb').read().strip()
            if not self.url.endswith('/'):
                self.url += '/'
        else:
            f = open(os.path.join(self.confdir, 'webport'), 'r')
            contents = f.read()
            f.close()
            fields = contents.split(':')
            proto, port = fields[:2]
            assert proto == 'tcp'
            port = int(port)
            self.url = 'http://localhost:%d' % (port,)

    def _init_rootdir(self):
        # For now we just use the same default as the CLI:
        rootdirfn = os.path.join(self.confdir, 'private', 'root_dir.cap')
        try:
            f = open(rootdirfn, 'r')
            cap = f.read().strip()
            f.close()
        except EnvironmentError, le:
            # FIXME: This user-friendly help message may be platform-dependent because it checks the exception description.
            if le.args[1].find('No such file or directory') != -1:
                raise SystemExit('%s requires a directory capability in %s, but it was not found.\n' % (sys.argv[0], rootdirfn))
            else:
                raise le

        self.rootdir = TahoeDir(self.url, canonicalize_cap(cap))

    def _get_node(self, path):
        assert path.startswith('/')
        if path == '/':
            return self.rootdir.resolve_path([])
        else:
            parts = path.split('/')[1:]
            return self.rootdir.resolve_path(parts)
    
    def _get_contents(self, path):
        contents = self.filecontents.get(path)
        if contents is None:
            node = self._get_node(path)
            contents = node.open().read()
            self.filecontents[path] = contents
        return contents
    
    @trace_calls
    @SystemError.wrap_returns
    def getattr(self, path):
        node = self._get_node(path)
        return node.getattr()
                
    @trace_calls
    @SystemError.wrap_returns
    def getdir(self, path):
        """
        return: [(name, typeflag), ... ]
        """
        node = self._get_node(path)
        return node.getdir()

    @trace_calls
    @SystemError.wrap_returns
    def mythread(self):
        return -errno.ENOSYS

    @trace_calls
    @SystemError.wrap_returns
    def chmod(self, path, mode):
        return -errno.ENOSYS

    @trace_calls
    @SystemError.wrap_returns
    def chown(self, path, uid, gid):
        return -errno.ENOSYS

    @trace_calls
    @SystemError.wrap_returns
    def fsync(self, path, isFsyncFile):
        return -errno.ENOSYS

    @trace_calls
    @SystemError.wrap_returns
    def link(self, target, link):
        return -errno.ENOSYS

    @trace_calls
    @SystemError.wrap_returns
    def mkdir(self, path, mode):
        return -errno.ENOSYS

    @trace_calls
    @SystemError.wrap_returns
    def mknod(self, path, mode, dev_ignored):
        return -errno.ENOSYS

    @trace_calls
    @SystemError.wrap_returns
    def open(self, path, mode):
        IgnoredFlags = os.O_RDONLY | os.O_NONBLOCK | os.O_SYNC | os.O_LARGEFILE 
        # Note: IgnoredFlags are all ignored!
        for fname in dir(os):
            if fname.startswith('O_'):
                flag = getattr(os, fname)
                if flag & IgnoredFlags:
                    continue
                elif mode & flag:
                    log('Flag not supported: %s', fname)
                    raise SystemError(errno.ENOSYS)

        self._get_contents(path)
        return 0

    @trace_calls
    @SystemError.wrap_returns
    def read(self, path, length, offset):
        return self._get_contents(path)[offset:length]

    @trace_calls
    @SystemError.wrap_returns
    def release(self, path):
        del self.filecontents[path]
        return 0

    @trace_calls
    @SystemError.wrap_returns
    def readlink(self, path):
        return -errno.ENOSYS

    @trace_calls
    @SystemError.wrap_returns
    def rename(self, oldpath, newpath):
        return -errno.ENOSYS

    @trace_calls
    @SystemError.wrap_returns
    def rmdir(self, path):
        return -errno.ENOSYS

    #@trace_calls
    @SystemError.wrap_returns
    def statfs(self):
        return -errno.ENOSYS

    @trace_calls
    @SystemError.wrap_returns
    def symlink ( self, targetPath, linkPath ):
        return -errno.ENOSYS

    @trace_calls
    @SystemError.wrap_returns
    def truncate(self, path, size):
        return -errno.ENOSYS

    @trace_calls
    @SystemError.wrap_returns
    def unlink(self, path):
        return -errno.ENOSYS

    @trace_calls
    @SystemError.wrap_returns
    def utime(self, path, times):
        return -errno.ENOSYS


class TahoeNode (object):
    NextInode = 0
    
    @staticmethod
    def make(baseurl, uri):
        typefield = uri.split(':', 2)[1]
        # FIXME: is this check correct?
        if uri.find('URI:DIR2') != -1:
            return TahoeDir(baseurl, uri)
        else:
            return TahoeFile(baseurl, uri)
        
    def __init__(self, baseurl, uri):
        if not baseurl.endswith('/'):
            baseurl += '/'
        self.burl = baseurl
        self.uri = uri
        self.fullurl = '%suri/%s' % (self.burl, self.uri)
        self.inode = TahoeNode.NextInode
        TahoeNode.NextInode += 1

    def getattr(self):
        """
        - st_mode (protection bits)
        - st_ino (inode number)
        - st_dev (device)
        - st_nlink (number of hard links)
        - st_uid (user ID of owner)
        - st_gid (group ID of owner)
        - st_size (size of file, in bytes)
        - st_atime (time of most recent access)
        - st_mtime (time of most recent content modification)
        - st_ctime (platform dependent; time of most recent metadata change on Unix,
                    or the time of creation on Windows).
        """
        # FIXME: Return metadata that isn't completely fabricated.
        return (self.get_mode(),
                self.inode,
                MagicDevNumber,
                self.get_linkcount(),
                os.getuid(),
                os.getgid(),
                self.get_size(),
                0,
                0,
                0)

    def get_metadata(self):
        f = self.open('?t=json')
        json = f.read()
        f.close()
        return simplejson.loads(json)
        
    def open(self, postfix=''):
        url = self.fullurl + postfix
        log('*** Fetching: %r', url)
        return urllib.urlopen(url)


class TahoeFile (TahoeNode):
    def __init__(self, baseurl, uri):
        #assert uri.split(':', 2)[1] in ('CHK', 'LIT'), `uri` # fails as of 0.7.0
        TahoeNode.__init__(self, baseurl, uri)

    # nonfuse:
    def get_mode(self):
        return stat.S_IFREG | 0400 # Read only regular file.

    def get_linkcount(self):
        return 1
    
    def get_size(self):
        rawsize = self.get_metadata()[1]['size']
        if type(rawsize) is not int: # FIXME: What about sizes which do not fit in python int?
            assert rawsize == u'?', `rawsize`
            return UnknownSize
        else:
            return rawsize
    
    def resolve_path(self, path):
        assert path == []
        return self
    

class TahoeDir (TahoeNode):
    def __init__(self, baseurl, uri):
        TahoeNode.__init__(self, baseurl, uri)

        self.mode = stat.S_IFDIR | 0500 # Read only directory.

    # FUSE:
    def getdir(self):
        d = [('.', self.get_mode()), ('..', self.get_mode())]
        for name, child in self.get_children().items():
            if name: # Just ignore this crazy case!
                d.append((name, child.get_mode()))
        return d

    # nonfuse:
    def get_mode(self):
        return stat.S_IFDIR | 0500 # Read only directory.

    def get_linkcount(self):
        return len(self.getdir())
    
    def get_size(self):
        return 2 ** 12 # FIXME: What do we return here?  len(self.get_metadata())
    
    def resolve_path(self, path):
        assert type(path) is list

        if path:
            head = path[0]
            child = self.get_child(head)
            return child.resolve_path(path[1:])
        else:
            return self
        
    def get_child(self, name):
        c = self.get_children()
        return c[name]

    def get_children(self):
        flag, md = self.get_metadata()
        assert flag == 'dirnode'

        c = {}
        for name, (childflag, childmd) in md['children'].items():
            if childflag == 'dirnode':
                cls = TahoeDir
            else:
                cls = TahoeFile

            c[str(name)] = cls(self.burl, childmd['ro_uri'])
        return c
        
        
def canonicalize_cap(cap):
    cap = urllib.unquote(cap)
    i = cap.find('URI:')
    assert i != -1, 'A cap must contain "URI:...", but this does not: ' + cap
    return cap[i:]
    

if __name__ == '__main__':
    main()

