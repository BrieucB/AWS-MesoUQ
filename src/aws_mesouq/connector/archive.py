"""Durable archives with closed internal source-hardlink proofs."""
from .archive_source import SOURCE_HELPERS

ARCHIVE_HELPER = SOURCE_HELPERS + r'''
import fcntl
root,token,operation=sys.argv[1:4]
assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',token)

def private(fd,directory=True,source_links=False):
    info=os.fstat(fd)
    assert info.st_uid==os.getuid() and not stat.S_IMODE(info.st_mode)&0o077
    assert stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if directory: assert stat.S_IMODE(info.st_mode)==0o700
    elif not source_links: assert info.st_nlink==1
    return info

def canonical(path):
    assert os.path.realpath(path)==path
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: private(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd

def beneath(fd,name,create=False):
    parts=src_safe(name)
    parent=os.dup(fd)
    try:
        for part in parts[:-1]:
            if create:
                try: os.mkdir(part,0o700,dir_fd=parent)
                except FileExistsError: pass
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            try: private(child)
            except BaseException:
                os.close(child)
                raise
            os.fsync(parent)
            os.close(parent)
            parent=child
        return parent,parts[-1]
    except BaseException:
        os.close(parent)
        raise

def verify(fd,digest,size,source=False,proven=False):
    assert isinstance(digest,str) and re.fullmatch(r'[a-f0-9]{64}',digest)
    assert type(size) is int and size>=0
    info=private(fd,False,source_links=source and proven)
    if not source: assert stat.S_IMODE(info.st_mode)==0o400
    before=src_stamp(info)
    assert info.st_size==size
    assert src_hash(fd,size)==digest, 'Archive checksum mismatch'
    assert src_stamp(private(fd,False,source_links=source and proven))==before


def copy_one(item):
    digest,size=item['sha256'],item['size_bytes']
    assert isinstance(digest,str) and re.fullmatch(r'[a-f0-9]{64}',digest)
    assert type(size) is int and size>=0
    target,name=beneath(archive,item['destination'],True)
    temporary='.'+name+'.partial'
    source=None
    peers=[]
    legacy=None
    try:
        try:
            existing=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=target)
        except FileNotFoundError:
            source=canonical(item['source_root'])
            source_identity=src_root_stamp(os.fstat(source))
            proof=item.get('source_link_group')
            if proof is not None:
                peers=src_group(item['source_root'],source,proof)
                assert item['source'] in proof['paths'], 'Requested source is absent from its proof'
                incoming=next(fd for path,_,_,fd in peers if path==item['source'])
            else:
                sp,sn,incoming=src_open(source,item['source'])
                legacy=(sp,sn,incoming)
            before=src_stamp(private(incoming,False,source_links=proof is not None))
            verify(incoming,digest,size,source=True,proven=proof is not None)
            if proof is not None:
                src_recheck(item['source_root'],source,proof,peers)
            else:
                assert before['nlink']==1
                src_check_root(item['source_root'],source,source_identity)
            try:
                stale=os.open(temporary,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=target)
            except FileNotFoundError: pass
            else:
                private(stale,False)
                os.close(stale)
                os.unlink(temporary,dir_fd=target)
                os.fsync(target)
            out=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o400,dir_fd=target)
            try:
                assert stat.S_IMODE(private(out,False).st_mode)==0o400
                os.lseek(incoming,0,os.SEEK_SET)
                h=hashlib.sha256()
                remaining=size
                while remaining:
                    block=os.read(incoming,min(1048576,remaining))
                    assert block, 'Source truncated during archive copy'
                    remaining-=len(block)
                    h.update(block)
                    view=memoryview(block)
                    while view:
                        written=os.write(out,view)
                        assert written>0
                        view=view[written:]
                assert not os.read(incoming,1), 'Source grew during archive copy'
                assert h.hexdigest()==digest, 'Source changed during archive copy'
                assert src_stamp(private(incoming,False,source_links=proof is not None))==before
                if proof is not None:
                    src_recheck(item['source_root'],source,proof,peers)
                else:
                    src_check_root(item['source_root'],source,source_identity)
                    fresh_parent,_,fresh=src_open(source,item['source'])
                    try: assert src_stamp(private(fresh,False))==before
                    finally:
                        os.close(fresh)
                        os.close(fresh_parent)
                assert os.fstat(out).st_size==size
                os.fsync(out)
            finally: os.close(out)
            os.link(temporary,name,src_dir_fd=target,dst_dir_fd=target,follow_symlinks=False)
            os.unlink(temporary,dir_fd=target)
            os.fsync(target)
            existing=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=target)
        try:
            temporary_stat=os.stat(temporary,dir_fd=target,follow_symlinks=False)
        except FileNotFoundError: pass
        else:
            installed=os.fstat(existing)
            assert stat.S_ISREG(installed.st_mode) and installed.st_uid==os.getuid()
            assert stat.S_IMODE(installed.st_mode)==0o400 and installed.st_nlink==2
            assert (temporary_stat.st_dev,temporary_stat.st_ino)==(installed.st_dev,installed.st_ino)
            os.unlink(temporary,dir_fd=target)
            os.fsync(target)
        verify(existing,digest,size)
        os.fsync(existing)
        os.fsync(target)
        os.fsync(archive)
        os.close(existing)
    finally:
        src_close(peers)
        if legacy is not None:
            os.close(legacy[2])
            os.close(legacy[0])
        if source is not None: os.close(source)
        os.close(target)

def verify_one(item):
    try:
        target,name=beneath(archive,item['destination'])
    except FileNotFoundError:
        raise SystemExit(3)
    try:
        try:
            existing=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=target)
        except FileNotFoundError:
            raise SystemExit(3)
        try:
            try:
                pending=os.stat('.'+name+'.partial',dir_fd=target,follow_symlinks=False)
            except FileNotFoundError: pass
            else:
                installed=os.fstat(existing)
                assert stat.S_ISREG(installed.st_mode) and installed.st_uid==os.getuid()
                assert stat.S_IMODE(installed.st_mode)==0o400 and installed.st_nlink==2
                assert (pending.st_dev,pending.st_ino)==(installed.st_dev,installed.st_ino)
                raise SystemExit(3)
            verify(existing,item['sha256'],item['size_bytes'])
            os.fsync(existing)
            os.fsync(target)
        finally: os.close(existing)
    finally: os.close(target)

def read_batch(operation):
    raw=sys.stdin.buffer.read(4194305)
    assert len(raw)<=4194304, 'Archive batch payload exceeds4MiB'
    values=json.loads(raw)
    assert isinstance(values,list) and 1<=len(values)<=32
    allowed={'source_root','source','destination','sha256','size_bytes','source_link_group'}
    required={'destination','sha256','size_bytes'}
    if operation=='copy-batch': required|={'source_root','source'}
    seen=set()
    for item in values:
        assert isinstance(item,dict) and required<=set(item)<=allowed
        src_safe(item['destination'])
        assert item['destination'] not in seen, 'Duplicate archive destination'
        seen.add(item['destination'])
        assert isinstance(item['sha256'],str) and re.fullmatch(r'[a-f0-9]{64}',item['sha256'])
        assert type(item['size_bytes']) is int and item['size_bytes']>=0
        if operation=='copy-batch':
            assert isinstance(item['source_root'],str) and os.path.isabs(item['source_root']) and len(item['source_root'])<=4096
            src_safe(item['source'])
            if 'source_link_group' in item: assert isinstance(item['source_link_group'],dict)
    return values
assert operation in ('prepare','copy','verify','copy-batch','verify-batch')
batch_items=read_batch(operation) if operation.endswith('-batch') else None
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
    copy_one(json.loads(sys.argv[4]))
elif operation=='verify':
    verify_one(json.loads(sys.argv[4]))
elif operation=='copy-batch':
    for item in batch_items:
        copy_one(item)
        verify_one(item)
elif operation=='verify-batch':
    incomplete=False
    for item in batch_items:
        try: verify_one(item)
        except SystemExit as error:
            if error.code!=3: raise
            incomplete=True
    if incomplete: raise SystemExit(3)
print(json.dumps({'verified':True}))
'''
