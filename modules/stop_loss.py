from .base_module import BaseModule

class StopLossModule(BaseModule):
    def __init__(self,cfg):
        super().__init__(cfg,'StopLoss')
        self.threshold=self.get_float('stop_loss_pct',5.0)/100
    def on_start(self): self.log('Threshold: '+str(round(self.threshold*100,1))+'%')
    def check(self,entry,current):
        if entry<=0: return False
        loss=(entry-current)/entry
        if loss>=self.threshold:
            self.emit('triggered',{'loss_pct':round(loss*100,2)})
            self.log('TRIGGERED',level='WARN'); return True
        return False