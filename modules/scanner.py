from .base_module import BaseModule

class ScannerModule(BaseModule):
    def __init__(self,cfg):
        super().__init__(cfg,'Scanner')
        self.min_liq=self.get_float('min_liquidity_usd',10000.0)
        self.min_holders=self.get_int('min_holders',100)
    def on_start(self): self.log('Min liq: $'+str(self.min_liq)+' | holders: '+str(self.min_holders))
    def evaluate(self,token):
        if token.get('liquidity_usd',0)<self.min_liq: return False
        if token.get('holders',0)<self.min_holders: return False
        self.emit('match',token); return True