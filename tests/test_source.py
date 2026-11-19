"""检查授权抽屉、原始收件表单与停用回执的来源契约。"""
from support import *
class Portal(SourceCase):
    def test_page_fields_and_receiver_receipts(self):
        page=self.browser();page.goto(self.base+'/');page.get_by_role('link',name='L-101 林一',exact=True).click();page.get_by_text('查看签署授权',exact=True).click();self.assertIn('c1',page.locator('details').inner_text())
        self.screenshot(page)
        page.goto(self.base+'/receiver/A');page.locator('[name=reference]').first.fill('source-check');page.locator('[name=file]').set_input_files({'name':'raw.json','mimeType':'application/json','buffer':json.dumps([{'id':'L-101'},{'id':'L-102'}]).encode()});page.get_by_role('button',name='交付名单').click();text=page.locator('body').inner_text();self.assertIn('accepted',text);self.assertIn('rejected',text)
        page.locator('form[action="/stop/A"] [name=reference]').fill('stop-check');page.locator('[name=delivery]').fill('source-check');page.locator('[name=id]').fill('L-101');page.locator('[name=fields]').fill('phone');page.get_by_role('button',name='申请停用').click();self.assertIn('pending',page.locator('body').inner_text())
        self.control(2);page.reload();self.assertIn('confirmed',page.locator('body').inner_text());page.goto(self.base+'/consultation/L-101');page.get_by_text('查看签署授权',exact=True).click();self.assertIn('c2',page.locator('details').inner_text())
        self.assertEqual(self.request('/__control',{'stage':0})[0],403)
