from .base_module import BaseModule

class WalletTrackerModule(BaseModule):
    def __init__(self,cfg):
        super().__init__(cfg,'WalletTracker')
        raw=self.get('tracked_wallets','')
        self.wallets=[w.strip() for w in raw.split(',') if w.strip()]
        self._seen=set()
    def on_start(self): self.log('Tracking '+str(len(self.wallets))+' wallet(s)')
    def process_tx(self,wallet,tx):
        h=tx.get('hash','')
        if h in self._seen: return
        self._seen.add(h); tx['wallet']=wallet
        self.emit('activity',tx)
        if tx.get('type') in('buy','open'): self.emit('new_position',tx)