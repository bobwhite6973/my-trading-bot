from .base_module import BaseModule

class TakeProfitModule(BaseModule):
    def __init__(self,cfg):
        super().__init__(cfg,'TakeProfit')
        self.target=self.get_float('take_profit_pct',15.0)/100
    def on_start(self): self.log('Target: '+str(round(self.target*100,1))+'%')
    def check(self,entry,current):
        if entry<=0: return False
        gain=(current-entry)/entry
        if gain>=self.target:
            self.emit('triggered',{'gain_pct':round(gain*100,2)})
            self.log('TRIGGERED'); return True
        return False