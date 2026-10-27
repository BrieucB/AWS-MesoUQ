"""Private durable archive transfer, executed by the verified remote Python.

Only explicitly bound files are copied. Each operation is bounded to one file,
uses descriptor-relative no-symlink traversal, and reopens the durable bytes.
"""

# Stdlib only: this helper is shipped over SSH and needs no HPC installation.
ARCHIVE_HELPER = r'''import fcntl,hashlib,json,os,stat,sys
root,token,operation=sys.argv[1:4]
def private(fd, directory=True):
    st=os.fstat(fd)
    assert st.st_uid==os.getuid() and not st.st_mode&0o077
    assert stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
    if directory:
        assert stat.S_IMODE(st.st_mode)==0o700
    if not directory:
        assert st.st_nlink==1
def canonical(path):
    assert os.path.realpath(path)==path
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    private(fd)
    return fd
def beneath(fd,name,create=False):
    parts=name.split('/')
    assert all(p and p not in ('.','..') and '/' not in p for p in parts)
    parent=os.dup(fd)
    for part in parts[:-1]:
        if create:
            try: os.mkdir(part,0o700,dir_fd=parent)
            except FileExistsError: pass
        child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
        private(child)
        os.fsync(parent)
        os.close(parent)
        parent=child
    return parent,parts[-1]
def verify(fd,digest,size):
    private(fd,False)
    assert os.fstat(fd).st_size==size
    os.lseek(fd,0,os.SEEK_SET)
    h=hashlib.sha256()
    remaining=size
    while remaining:
        block=os.read(fd,min(1024*1024,remaining))
        assert block, 'File truncated during verification'
        remaining-=len(block)
        h.update(block)
    assert not os.read(fd,1), 'File grew during verification'
    assert h.hexdigest()==digest, 'Archive checksum mismatch'
parent=canonical(root)
try:
    os.mkdir(token,0o700,dir_fd=parent)
    os.fsync(parent)
except FileExistsError: pass
archive=os.open(token,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
private(archive)
lock=os.open('.archive.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=archive)
private(lock,False)
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
if operation=='copy':
    item=json.loads(sys.argv[4])
    digest,size=item['sha256'],item['size_bytes']
    assert len(digest)==64 and size>=0
    target,name=beneath(archive,item['destination'],True)
    temporary='.'+name+'.partial'
    try:
        existing=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=target)
    except FileNotFoundError:
        source=canonical(item['source_root'])
        sp,sn=beneath(source,item['source'])
        incoming=os.open(sn,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=sp)
        verify(incoming,digest,size)
        try:
            stale=os.open(temporary,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=target)
        except FileNotFoundError: pass
        else:
            private(stale,False)
            os.close(stale)
            os.unlink(temporary,dir_fd=target)
            os.fsync(target)
        out=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o400,dir_fd=target)
        os.lseek(incoming,0,os.SEEK_SET)
        remaining=size
        while remaining:
            block=os.read(incoming,min(1024*1024,remaining))
            assert block, 'Source truncated during archive copy'
            remaining-=len(block)
            view=memoryview(block)
            while view:
                view=view[os.write(out,view):]
        assert not os.read(incoming,1), 'Source grew during archive copy'
        os.fsync(out)
        os.close(out)
        # Atomic no-clobber installation. Any pre-existing different target fails.
        os.link(temporary,name,src_dir_fd=target,dst_dir_fd=target,follow_symlinks=False)
        os.unlink(temporary,dir_fd=target)
        os.fsync(target)
        existing=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=target)
    # A disconnect after link() but before unlink() leaves two names for the
    # same inode. Finish that exact installation without replacing any bytes.
    try:
        temporary_stat=os.stat(temporary,dir_fd=target,follow_symlinks=False)
    except FileNotFoundError: pass
    else:
        installed=os.fstat(existing)
        assert (temporary_stat.st_dev,temporary_stat.st_ino)==(installed.st_dev,installed.st_ino)
        os.unlink(temporary,dir_fd=target)
        os.fsync(target)
    verify(existing,digest,size)
    os.fsync(existing)
    os.fsync(target)
    os.fsync(archive)
    os.close(existing)
elif operation=='verify':
    item=json.loads(sys.argv[4])
    target,name=beneath(archive,item['destination'])
    existing=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=target)
    verify(existing,item['sha256'],item['size_bytes'])
    os.fsync(existing)
    os.fsync(target)
    os.close(existing)
elif operation!='prepare':
    raise ValueError('Unsupported archive operation')
print(json.dumps({'verified':True}))
'''
