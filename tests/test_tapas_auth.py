import unittest

from comic_archive.tapas_auth import identity


class TapasSignInTests(unittest.TestCase):
    def test_only_a_positive_integer_account_identity_is_accepted(self):
        for value in (None,[],{}, {'data':None},{'data':{'user':{'id':True}}},
                      {'data':{'user':{'id':'11'}}},{'data':{'user':{'id':0}}},
                      {'data':{'user':{'id':11}},'error_details':'PRIVATE'}):
            self.assertIsNone(identity(value))
        self.assertEqual(identity({'data':{'user':{'id':11}}}),'11')
