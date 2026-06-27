import time

class BaseModule:
    def __init__(self,cfg,name=None):
        self.cfg=cfg; self.name=name or self.__class__.__name__
        self._running=False; self._listeners={}; self._start_time=None
    def start(self):
        if self._running: return
        self._running=True; self._start_time=time.time()
        self.log('Starting...'); self.on_start(); self.log('Active')
    def stop(self): self._running=False; self.on_stop(); self.log('Stopped')
    def on_start(self): pass
    def on_stop(self): pass
    def on(self,event,cb): self._listeners.setdefault(event,[]).append(cb)
    def emit(self,event,data=None):
        for cb in self._listeners.get(event,[]): cb(data)
    def get(self,k,d=None): return self.cfg.get(k,d)
    def get_float(self,k,d=0.0):
        try: return float(self.cfg.get(k,d))
        except: return d
    def get_int(self,k,d=0):
        try: return int(self.cfg.get(k,d))
        except: return d
    def log(self,msg,level='INFO'):
        print('['+time.strftime('%H:%M:%S')+'] ['+level+'] ['+self.name+'] '+msg)