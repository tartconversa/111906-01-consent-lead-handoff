"""咨询登记与展商收件门户的独立模拟页面。"""
from .common import *
class App(Application):
    def handle(self,h):
        p=h.path_only
        if p=='/': return self.document(h,'咨询登记台',''.join('<p><a href="/consultation/'+c['id']+'">'+c['id']+' '+c['name']+'</a></p>' for c in self.data('cards'))+'<a href="/receiver/A">展商A收件台</a> <a href="/receiver/B">展商B收件台</a>')
        if p.startswith('/consultation/'):
            key=p.split('/')[-1];card=next(c for c in (self.data('cards') or self.seed['stages'][0]['cards']) if c['id']==key)
            return self.document(h,'咨询卡 '+key,view(card)+'<details><summary>查看签署授权</summary>'+view(self.data('consents')[key])+'</details>')
        if p.startswith('/receiver/'):
            vendor=p.split('/')[2];bucket='delivery-'+vendor
            if h.command=='POST':
                ref=h.body['reference'];rows=json.loads(h.files['file']['bytes'])
                item=self.record(bucket,ref,rows)
                if 'rows' not in item: item['rows']=[{'id':r['id'],'state':'rejected' if r['id'] in self.data('reject') else 'accepted'} for r in rows]
                if h.body.get('lose_response')=='yes': return h.send(b'','text/plain',503)
            return self.document(h,'展商'+vendor+'收件历史',view(self.remote.get(bucket,{}))+form(p,[('reference','交付引用','text',''),('file','JSON名单','file',''),('lose_response','丢失确认（yes）','text','')],'交付名单')+form('/stop/'+vendor,[('reference','停用引用','text',''),('delivery','原交付引用','text',''),('id','咨询号','text',''),('fields','停用字段逗号分隔','text','')],'申请停用'))
        if p.startswith('/stop/'):
            vendor=p.split('/')[-1]
            if h.command=='POST': self.record('stop-'+vendor,h.body['reference'],dict(h.body))
            for item in self.remote.get('stop-'+vendor,{}).values(): item['state']='confirmed' if self.data('stop_confirm') else 'pending'
            return self.document(h,'停用回执',view(self.remote.get('stop-'+vendor,{})))
        return self.emit(h,{'error':'not_found'},404)
if __name__=='__main__': main(App())
