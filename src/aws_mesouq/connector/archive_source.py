"""Descriptor-based source inventories; inode proofs never enter public manifests."""

SOURCE_HELPERS = r'''import hashlib,json,os,re,stat,sys
SRC_FILE_KEYS={'dev','ino','uid','mode','nlink','size','mtime_ns','ctime_ns'}
SRC_ROOT_KEYS={'dev','ino','uid','mode'}
SRC_MAX_FILES=10001
SRC_MAX_NODES=20000
SRC_MAX_PEERS=128
SRC_MAX_DEPTH=64

def src_safe(name):
    assert isinstance(name,str) and 0<len(name)<=1024
    parts=name.split('/')
    assert len(parts)<=SRC_MAX_DEPTH
    assert all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',p) and p not in ('.','..') for p in parts)
    return parts

def src_private(fd,directory=False):
    info=os.fstat(fd)
    assert info.st_uid==os.getuid() and not stat.S_IMODE(info.st_mode)&0o077
    assert stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return info

def src_stamp(info):
    return {'dev':info.st_dev,'ino':info.st_ino,'uid':info.st_uid,
            'mode':stat.S_IMODE(info.st_mode),'nlink':info.st_nlink,
            'size':info.st_size,'mtime_ns':info.st_mtime_ns,'ctime_ns':info.st_ctime_ns}

def src_root_stamp(info):
    return {k:v for k,v in src_stamp(info).items() if k in SRC_ROOT_KEYS}

def src_root(path):
    assert isinstance(path,str) and os.path.isabs(path) and os.path.realpath(path)==path
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        info=src_private(fd,True)
        assert stat.S_IMODE(info.st_mode)==0o700
    except BaseException:
        os.close(fd)
        raise
    return fd

def src_parent(root,name):
    parts=src_safe(name)
    parent=os.dup(root)
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            try: src_private(child,True)
            except BaseException:
                os.close(child)
                raise
            os.close(parent)
            parent=child
        return parent,parts[-1]
    except BaseException:
        os.close(parent)
        raise

def src_open(root,name):
    parent,base=src_parent(root,name)
    try:
        fd=os.open(base,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        try: src_private(fd)
        except BaseException:
            os.close(fd)
            raise
        return parent,base,fd
    except BaseException:
        os.close(parent)
        raise

def src_open_dir(root,name):
    if not name:
        return os.dup(root)
    parent,base=src_parent(root,name)
    try:
        fd=os.open(base,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
        try: src_private(fd,True)
        except BaseException:
            os.close(fd)
            raise
        return fd
    finally: os.close(parent)

def src_hash(fd,size):
    assert type(size) is int and size>=0
    os.lseek(fd,0,os.SEEK_SET)
    h=hashlib.sha256()
    remaining=size
    while remaining:
        block=os.read(fd,min(1048576,remaining))
        assert block, 'Source truncated during verification'
        remaining-=len(block)
        h.update(block)
    assert not os.read(fd,1), 'Source grew during verification'
    return h.hexdigest()

def src_check_root(path,fd,expected):
    assert os.path.realpath(path)==path
    assert src_root_stamp(src_private(fd,True))==expected
    current=os.stat(path,follow_symlinks=False)
    assert stat.S_ISDIR(current.st_mode) and src_root_stamp(current)==expected

def src_close(peers):
    for _,parent,_,fd in reversed(peers):
        os.close(fd)
        os.close(parent)

def src_recheck(path,root,proof,peers):
    src_check_root(path,root,proof['root'])
    for name,parent,base,fd in peers:
        assert src_stamp(src_private(fd))==proof['file']
        at_path=os.stat(base,dir_fd=parent,follow_symlinks=False)
        assert stat.S_ISREG(at_path.st_mode) and src_stamp(at_path)==proof['file']
        fresh_parent,_,fresh=src_open(root,name)
        try: assert src_stamp(src_private(fresh))==proof['file']
        finally:
            os.close(fresh)
            os.close(fresh_parent)

def src_group(path,root,proof):
    assert isinstance(proof,dict) and set(proof)=={'root','file','paths'}
    assert isinstance(proof['root'],dict) and set(proof['root'])==SRC_ROOT_KEYS
    assert isinstance(proof['file'],dict) and set(proof['file'])==SRC_FILE_KEYS
    assert all(type(v) is int and v>=0 for v in proof['root'].values())
    assert all(type(v) is int and v>=0 for v in proof['file'].values())
    names=proof['paths']
    assert isinstance(names,list) and 2<=len(names)<=SRC_MAX_PEERS
    assert all(isinstance(name,str) for name in names)
    assert names==sorted(set(names))
    assert proof['file']['nlink']==len(names)
    peers=[]
    try:
        src_check_root(path,root,proof['root'])
        for name in names:
            parent,base,fd=src_open(root,name)
            peers.append((name,parent,base,fd))
            assert src_stamp(src_private(fd))==proof['file']
        src_recheck(path,root,proof,peers)
        return peers
    except BaseException:
        src_close(peers)
        raise
'''

TREE_HELPER = SOURCE_HELPERS + r'''
root=sys.argv[1]
rootfd=src_root(root)
root_identity=src_root_stamp(os.fstat(rootfd))
files={}
groups={}
directories=[]
node_count=0

def walk(fd,prefix='',depth=0):
    global node_count
    assert depth<=SRC_MAX_DEPTH
    before=src_stamp(src_private(fd,True))
    directories.append((prefix,before))
    names=[]
    with os.scandir(fd) as scan:
        for entry in scan:
            assert len(names)<SRC_MAX_NODES
            names.append(entry.name)
    for name in sorted(names):
        node_count+=1
        assert node_count<=SRC_MAX_NODES
        relative=prefix+'/'+name if prefix else name
        src_safe(relative)
        original=os.stat(name,dir_fd=fd,follow_symlinks=False)
        assert original.st_uid==os.getuid() and not stat.S_IMODE(original.st_mode)&0o077
        if stat.S_ISDIR(original.st_mode):
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            try:
                assert src_stamp(src_private(child,True))==src_stamp(original)
                walk(child,relative,depth+1)
            finally: os.close(child)
        else:
            assert stat.S_ISREG(original.st_mode), 'Source contains a nonregular entry'
            incoming=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
            try:
                info=src_private(incoming)
                stamp=src_stamp(info)
                assert stamp==src_stamp(original)
                digest=src_hash(incoming,info.st_size)
                assert src_stamp(src_private(incoming))==stamp
                assert src_stamp(os.stat(name,dir_fd=fd,follow_symlinks=False))==stamp
            finally: os.close(incoming)
            assert len(files)<SRC_MAX_FILES
            files[relative]=({'path':relative,'sha256':digest,'size_bytes':info.st_size},stamp)
            groups.setdefault((info.st_dev,info.st_ino),[]).append(relative)
    assert src_stamp(src_private(fd,True))==before

try:
    walk(rootfd)
    for name,expected in directories:
        fd=src_open_dir(rootfd,name)
        try: assert src_stamp(src_private(fd,True))==expected
        finally: os.close(fd)
    for name,(_,expected) in files.items():
        parent,_,fd=src_open(rootfd,name)
        try: assert src_stamp(src_private(fd))==expected
        finally:
            os.close(fd)
            os.close(parent)
    proofs=[]
    for paths in groups.values():
        paths=sorted(paths)
        stamp=files[paths[0]][1]
        assert len(paths)==stamp['nlink'], 'Source inode has an external or omitted alias'
        assert all(files[name][1]==stamp for name in paths)
        if len(paths)>1:
            proof={'root':root_identity,'file':stamp,'paths':paths}
            peers=src_group(root,rootfd,proof)
            try: src_recheck(root,rootfd,proof,peers)
            finally: src_close(peers)
            proofs.append(proof)
    src_check_root(root,rootfd,root_identity)
    print(json.dumps({'version':1,'files':[files[name][0] for name in sorted(files)],
                      'source_link_groups':sorted(proofs,key=lambda p:p['paths'])},allow_nan=False))
finally: os.close(rootfd)
'''
