"""Application-scoped, dynamic Windows Filtering Platform isolation for a local test copy."""
import ctypes as C,uuid
from ctypes import wintypes as W
class GUID(C.Structure):
 _fields_=[('d1',C.c_uint32),('d2',C.c_uint16),('d3',C.c_uint16),('d4',C.c_ubyte*8)]
 @classmethod
 def of(cls,s):return cls.from_buffer_copy(uuid.UUID(s).bytes_le)
class DISPLAY(C.Structure):_fields_=[('name',W.LPWSTR),('description',W.LPWSTR)]
class BLOB(C.Structure):_fields_=[('size',C.c_uint32),('data',C.POINTER(C.c_ubyte))]
class VALUE_UNION(C.Union):_fields_=[('u8',C.c_uint8),('u32',C.c_uint32),('ptr',C.c_void_p)]
class VALUE(C.Structure):_anonymous_=('u',);_fields_=[('type',C.c_uint32),('u',VALUE_UNION)]
class SESSION(C.Structure):_fields_=[('key',GUID),('display',DISPLAY),('flags',C.c_uint32),('timeout',C.c_uint32),('pid',W.DWORD),('sid',C.c_void_p),('username',W.LPWSTR),('kernel',W.BOOL)]
class CONDITION(C.Structure):_fields_=[('key',GUID),('match',C.c_uint32),('value',VALUE)]
class ACTION(C.Structure):_fields_=[('type',C.c_uint32),('key',GUID)]
class CONTEXT(C.Union):_fields_=[('raw',C.c_uint64),('key',GUID)]
class FILTER(C.Structure):_fields_=[('key',GUID),('display',DISPLAY),('flags',C.c_uint32),('provider',C.c_void_p),('data',BLOB),('layer',GUID),('sublayer',GUID),('weight',VALUE),('count',C.c_uint32),('conditions',C.POINTER(CONDITION)),('action',ACTION),('context',CONTEXT),('reserved',C.c_void_p),('id',C.c_uint64),('effectiveWeight',VALUE)]
APP_ID=GUID.of('d78e1e87-8644-4ea5-9437-d809ecefc971')
UNIVERSAL=GUID.of('eebecc03-ced4-4380-819a-2734397b2b74')
LAYERS=['c38d57d1-05a7-4c33-904f-7fbceee60e82','4a72393b-319f-44bc-84c3-ba54dcb3b6b4','e1cd9fe7-f4b5-4273-96c0-592e487b8650','a3b42c97-9f04-4672-b87e-cee9c483257f']
class Isolation:
 def __init__(self):
  self.dll=C.WinDLL('fwpuclnt');self.handle=W.HANDLE();self.ids=[]
  specs={'FwpmEngineOpen0':([W.LPCWSTR,C.c_uint32,C.c_void_p,C.POINTER(SESSION),C.POINTER(W.HANDLE)],W.DWORD),'FwpmEngineClose0':([W.HANDLE],W.DWORD),'FwpmGetAppIdFromFileName0':([W.LPCWSTR,C.POINTER(C.POINTER(BLOB))],W.DWORD),'FwpmFilterAdd0':([W.HANDLE,C.POINTER(FILTER),C.c_void_p,C.POINTER(C.c_uint64)],W.DWORD),'FwpmFilterGetById0':([W.HANDLE,C.c_uint64,C.POINTER(C.POINTER(FILTER))],W.DWORD),'FwpmFreeMemory0':([C.POINTER(C.c_void_p)],None)}
  for name,(args,restype) in specs.items():f=getattr(self.dll,name);f.argtypes=args;f.restype=restype
  self.session=SESSION();self.session.key=GUID.of(str(uuid.uuid4()));self.session.display.name='Pengfei local test isolation';self.session.flags=1
  self.check(self.dll.FwpmEngineOpen0(None,10,None,C.byref(self.session),C.byref(self.handle)),'FwpmEngineOpen0')
 @staticmethod
 def check(code,op):
  if code:raise OSError(f'{op}: 0x{code:08x}')
 def free(self,p):self.dll.FwpmFreeMemory0(C.cast(C.byref(p),C.POINTER(C.c_void_p)))
 def add(self,path):
  app=C.POINTER(BLOB)();self.check(self.dll.FwpmGetAppIdFromFileName0(str(path),C.byref(app)),'FwpmGetAppIdFromFileName0')
  try:
   cond=CONDITION();cond.key=APP_ID;cond.match=0;cond.value.type=12;cond.value.ptr=C.cast(app,C.c_void_p).value
   for layer in LAYERS:
    f=FILTER();f.key=GUID.of(str(uuid.uuid4()));f.display.name='Pengfei isolated test';f.layer=GUID.of(layer);f.sublayer=UNIVERSAL;f.weight.type=1;f.weight.u8=15;f.count=1;f.conditions=C.pointer(cond);f.action.type=0x1001
    fid=C.c_uint64();self.check(self.dll.FwpmFilterAdd0(self.handle,C.byref(f),None,C.byref(fid)),'FwpmFilterAdd0')
    out=C.POINTER(FILTER)();self.check(self.dll.FwpmFilterGetById0(self.handle,fid.value,C.byref(out)),'FwpmFilterGetById0')
    try:
     assert out.contents.action.type==0x1001 and out.contents.count==1
    finally:self.free(out)
    self.ids.append(fid.value)
  finally:self.free(app)
 def close(self):
  if self.handle:self.dll.FwpmEngineClose0(self.handle);self.handle=None
