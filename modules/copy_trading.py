from .base_module import BaseModule

class CopyTradingModule(BaseModule):
    def __init__(self,cfg):
        super().__init__(cfg,'CopyTrading')
        self.source_wallet=self.get('source_wallet','')
        self.copy_ratio=self.get_float('copy_ratio',1.0)
        self._execute_fn=None; self._risk=None
    def on_start(self): self.log('Copying: '+self.source_wallet[:12]+'...')
    def set_executor(self,fn): self._execute_fn=fn
    def set_risk(self,r): self._risk=r
    def on_activity(self,tx):
        if tx.get('wallet')!=self.source_wallet: return
        size=tx.get('amount_usd',0)*self.copy_ratio
        if self._risk and not self._risk.approve(size): return
        if self._execute_fn: self._execute_fn({**tx,'size_usd':size})
        self.emit('copied',tx)