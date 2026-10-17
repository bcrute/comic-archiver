from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from comic_archive.hosted import Workspace
from comic_archive.inventory import Inventory
from comic_archive.tapas_auth import SignIn,identity
from comic_archive.network import NetworkPolicyHold


class TapasSignInTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.workspace=Workspace('a'*32,Path(temporary.name))
        self.workspace.session.mkdir();self.workspace.session.joinpath('state.json').write_text('OLD_PRIVATE_SESSION')
        with Inventory(self.workspace.library):pass
        self.signin=SignIn(self.workspace,'firefox')
        self.driver=Mock();self.browser=Mock();self.context=self.browser.new_context.return_value
        self.context.request.get.return_value.json.return_value={'data':{'user':{'id':11}},'error_details':None}
        self.page=self.context.new_page.return_value
        factory=Mock();factory.__enter__=Mock(return_value=self.driver);factory.__exit__=Mock(return_value=False)
        values={'browser_api':Mock(return_value=Mock(return_value=factory)),
                'launch_browser':Mock(return_value=self.browser),'save_session':Mock(),
                'NetworkGuard':Mock(),'background.stop':Mock(return_value={'running':False}),
                'background.start':Mock(return_value={'running':True})}
        self.mocks={}
        for name,value in values.items():
            wrapper=patch('comic_archive.tapas_auth.'+name,value);wrapper.start();self.addCleanup(wrapper.stop)
            self.mocks[name]=value

    def test_automatic_browser_login_has_no_terminal_prompt_and_starts_worker_without_source_jobs(self):
        response=self.context.request.get.return_value
        response.json.side_effect=[{'data':{'user':None}},{'data':{'user':{'id':11}}}]
        with patch('builtins.input',side_effect=AssertionError('No terminal input')):
            self.signin._run()
        self.assertEqual(self.signin.status(),{'status':'connected'})
        self.page.goto.assert_called_once_with('https://tapas.io/account/signin',timeout=30000)
        self.page.wait_for_timeout.assert_called_once_with(3000)
        self.mocks['save_session'].assert_called_once_with(self.context,self.workspace.session)
        self.mocks['background.start'].assert_called_once_with(self.workspace.session,'firefox',0.25)
        self.assertFalse(list(self.workspace.session.glob('worker/requests/*.json')))
        self.browser.close.assert_called_once()

    def test_wrong_account_keeps_existing_session_and_worker_intact(self):
        with Inventory(self.workspace.library) as inventory:inventory.bind_account('22')
        self.signin._run()
        self.assertEqual(self.signin.status(),{'status':'error','code':'account_mismatch'})
        self.mocks['save_session'].assert_not_called();self.mocks['background.stop'].assert_not_called()
        self.assertEqual(self.workspace.session.joinpath('state.json').read_text(),'OLD_PRIVATE_SESSION')

    def test_worker_drain_failure_and_private_browser_errors_are_fixed_status_only(self):
        self.mocks['background.stop'].return_value={'running':True,'stop_wait_expired':True}
        self.signin._run();self.assertEqual(self.signin.status(),{'status':'error','code':'signin_failed'})
        self.mocks['save_session'].assert_not_called()
        self.mocks['launch_browser'].side_effect=RuntimeError('PRIVATE_COOKIE /PRIVATE_PATH')
        self.signin._run();self.assertEqual(self.signin.status(),{'status':'error','code':'signin_failed'})

    def test_network_hold_never_opens_browser_or_alters_saved_session(self):
        self.mocks['NetworkGuard'].return_value.check.side_effect=NetworkPolicyHold('PRIVATE')
        self.signin._run()
        self.assertEqual(self.signin.status(),{'status':'error','code':'network_held'})
        self.mocks['launch_browser'].assert_not_called();self.mocks['save_session'].assert_not_called()

    def test_cancel_timeout_and_duplicate_clicks_keep_saved_state(self):
        self.signin._cancel.set();self.signin._run()
        self.assertEqual(self.signin.status()['status'],'cancelled')
        self.mocks['save_session'].assert_not_called()
        self.signin._cancel.clear()
        with patch('comic_archive.tapas_auth.time.monotonic',side_effect=[0,1000]):self.signin._run()
        self.assertEqual(self.signin.status(),{'status':'error','code':'signin_incomplete'})
        with patch('comic_archive.tapas_auth.threading.Thread') as thread:
            thread.return_value.is_alive.return_value=True
            self.assertEqual(self.signin.start(),{'status':'running'})
            self.assertEqual(self.signin.start(),{'status':'running'})
            thread.assert_called_once();thread.return_value.start.assert_called_once()
            self.signin.close();thread.return_value.join.assert_called_once_with(timeout=3)

    def test_cancel_while_the_previous_worker_stops_saves_nothing_and_starts_no_worker(self):
        self.mocks['background.stop'].side_effect=lambda *args,**kwargs:self.signin._cancel.set() or {'running':False}
        self.signin._run()
        self.assertEqual(self.signin.status(),{'status':'cancelled','code':'signin_incomplete'})
        self.mocks['save_session'].assert_not_called();self.mocks['background.start'].assert_not_called()

    def test_only_a_positive_integer_account_identity_is_accepted(self):
        for value in (None,[],{}, {'data':None},{'data':{'user':{'id':True}}},
                      {'data':{'user':{'id':'11'}}},{'data':{'user':{'id':0}}},
                      {'data':{'user':{'id':11}},'error_details':'PRIVATE'}):
            self.assertIsNone(identity(value))
        self.assertEqual(identity({'data':{'user':{'id':11}}}),'11')

    def test_a_failed_identity_check_is_retried_without_publishing_its_error(self):
        good=Mock();good.json.return_value={'data':{'user':{'id':11}}}
        self.context.request.get.side_effect=[ConnectionError('PRIVATE_COOKIE'),good]
        self.signin._run()
        self.assertEqual(self.signin.status(),{'status':'connected'})
        self.assertEqual(self.context.request.get.call_count,2)
        self.page.wait_for_timeout.assert_called_once_with(3000)
        self.mocks['save_session'].assert_called_once_with(self.context,self.workspace.session)

    def test_cancel_during_the_identity_check_discards_even_a_valid_answer(self):
        good=Mock();good.json.return_value={'data':{'user':{'id':11}}}
        self.context.request.get.side_effect=lambda *args,**kwargs:(self.signin._cancel.set(),good)[1]
        self.signin._run()
        self.assertEqual(self.signin.status(),{'status':'cancelled','code':'signin_incomplete'})
        self.mocks['background.stop'].assert_not_called();self.mocks['save_session'].assert_not_called()
        self.mocks['background.start'].assert_not_called()
        self.assertEqual(self.workspace.session.joinpath('state.json').read_text(),'OLD_PRIVATE_SESSION')
        self.browser.close.assert_called_once()

    def test_closing_before_any_sign_in_started_is_harmless_and_does_not_block_a_later_one(self):
        self.signin.close()
        self.assertEqual(self.signin.status(),{'status':'idle'})
        self.assertTrue(self.signin._cancel.is_set())
        with patch('comic_archive.tapas_auth.threading.Thread') as thread:
            self.assertEqual(self.signin.start(),{'status':'running'})
        self.assertFalse(self.signin._cancel.is_set())
        thread.return_value.start.assert_called_once()

