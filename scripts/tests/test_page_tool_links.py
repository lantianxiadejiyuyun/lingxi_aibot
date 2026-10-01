"""AI page outputs must use the configured public address across tool actions."""
from types import SimpleNamespace
from unittest.mock import patch

from flask_login import login_user

from scripts.tests.support import IsolatedAppTestCase
from app.ai.tools import page_tools
from app.services import page_service, image_service
from app.services.settings_service import set_setting


class PageToolLinkTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        set_setting('page_public_base_url', 'http://8.8.8.8:30079', user_id=0)
        set_setting('page_port', 8080, user_id=0)
        set_setting('page_host', '192.168.1.100', user_id=0)
        self.root = 'http://8.8.8.8:30079'

    def test_create_get_list_update_and_copy_return_canonical_public_links(self):
        with self.app.test_request_context('/', base_url='http://192.168.1.100:8000'), \
                patch('app.services.page_service._rag_index'), \
                patch('app.utils.netinfo.feishu_sdk_page_warning', return_value=''):
            login_user(self.user)
            created = page_tools.create_page('Online page', '<html>online</html>', slug='public-link-test')
            expected = self.root + '/webs/html/public-link-test'
            self.assertIn(expected, created)
            page = page_service.list_pages(self.user.id)[0]
            self.assertEqual(page_tools.get_page(page.id)['url'], expected)
            self.assertEqual(page_tools.list_pages()[0]['url'], expected)
            updated = page_tools.update_page(page.id, slug='updated-public-link')
            self.assertIn(self.root + '/webs/html/updated-public-link', updated)
            copied = page_tools.duplicate_page(page.id)
            copies = [item for item in page_service.list_pages(self.user.id) if item.id != page.id]
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0].user_id, self.user.id)
            self.assertIn(self.root + '/webs/html/' + copies[0].slug, copied)

    def test_private_page_tool_keeps_authenticated_origin(self):
        set_setting('admin_entry', 'private-entry', user_id=0)
        with self.app.test_request_context('/', base_url='http://192.168.1.100:8000'), \
                patch('app.services.page_service._rag_index'), \
                patch('app.utils.netinfo.feishu_sdk_page_warning', return_value=''):
            login_user(self.user)
            result = page_tools.create_page('Private', '<html>private</html>', slug='private-link-test', is_public=False)
            self.assertIn('http://192.168.1.100:8000/private-entry/webs/html/private-link-test', result)
            self.assertNotIn(self.root, result)

    def test_public_images_use_public_origin_but_private_images_keep_login_path(self):
        asset = SimpleNamespace(file_path='img-abcd1234-1.png', is_public=True)
        self.assertEqual(image_service.asset_url(asset), self.root + '/img/img-abcd1234-1.png')
        asset.is_public = False
        self.app.config['ADMIN_ENTRY'] = 'private-entry'
        with self.app.test_request_context('/', base_url='http://192.168.1.100:8000'):
            self.assertEqual(image_service.asset_url(asset), 'http://192.168.1.100:8000/private-entry/img/img-abcd1234-1.png')
