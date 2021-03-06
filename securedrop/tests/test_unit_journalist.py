#!/usr/bin/env python
# -*- coding: utf-8 -*-

from cStringIO import StringIO
import os
import random
import time
import unittest
import zipfile

from flask import url_for, escape
from flask_testing import TestCase

# Set environment variable so config.py uses a test environment
os.environ['SECUREDROP_ENV'] = 'test'
import config
import crypto_util
from db import (db_session, InvalidPasswordLength, Journalist, Reply, Source,
                Submission)
import journalist
import utils

# Smugly seed the RNG for deterministic testing
random.seed('¯\_(ツ)_/¯')

class TestJournalistApp(TestCase):

    # A method required by flask_testing.TestCase
    def create_app(self):
        return journalist.app

    def setUp(self):
        utils.env.setup()

        # Patch the two-factor verification to avoid intermittent errors
        utils.db_helper.mock_verify_token(self)

        # Setup test users: user & admin
        self.user, self.user_pw = utils.db_helper.init_journalist()
        self.admin, self.admin_pw = utils.db_helper.init_journalist(
            is_admin=True)

    def tearDown(self):
        utils.env.teardown()

    def test_unauthorized_access_redirects_to_login(self):
        resp = self.client.get(url_for('index'))
        self.assertRedirects(resp, url_for('login'))

    def test_invalid_credentials(self):
        resp = self.client.post(url_for('login'),
                                data=dict(username=self.user.username,
                                          password='invalid',
                                          token='mocked'))
        self.assert200(resp)
        self.assertIn("Login failed", resp.data)

    def test_valid_credentials(self):
        resp = self.client.post(url_for('login'),
                                data=dict(username=self.user.username,
                                          password=self.user_pw,
                                          token='mocked'),
                                follow_redirects=True)
        self.assert200(resp) # successful login redirects to index
        self.assertIn("Sources", resp.data)
        self.assertIn("No documents have been submitted!", resp.data)

    def test_admin_login_redirects_to_index(self):
        resp = self.client.post(url_for('login'),
                                data=dict(username=self.admin.username,
                                          password=self.admin_pw,
                                          token='mocked'))
        self.assertRedirects(resp, url_for('index'))

    def test_user_login_redirects_to_index(self):
        resp = self.client.post(url_for('login'),
                                data=dict(username=self.user.username,
                                          password=self.user_pw,
                                          token='mocked'))
        self.assertRedirects(resp, url_for('index'))

    def test_admin_has_link_to_edit_account_page_in_index_page(self):
        resp = self.client.post(url_for('login'),
                               data=dict(username=self.admin.username,
                                         password=self.admin_pw,
                                         token='mocked'),
                               follow_redirects=True)
        edit_account_link = '<a href="{}">{}</a>'.format(url_for('edit_account'),
                                                         "Edit Account")
        self.assertIn(edit_account_link, resp.data)

    def test_user_has_link_to_edit_account_page_in_index_page(self):
        resp = self.client.post(url_for('login'),
                               data=dict(username=self.user.username,
                                         password=self.user_pw,
                                         token='mocked'),
                               follow_redirects=True)
        edit_account_link = '<a href="{}">{}</a>'.format(url_for('edit_account'),
                                                         "Edit Account")
        self.assertIn(edit_account_link, resp.data)


    def test_admin_has_link_to_admin_index_page_in_index_page(self):
        resp = self.client.post(url_for('login'),
                               data=dict(username=self.admin.username,
                                         password=self.admin_pw,
                                         token='mocked'),
                               follow_redirects=True)
        admin_link = '<a href="{}">{}</a>'.format(url_for('admin_index'),
                                                  "Admin")
        self.assertIn(admin_link, resp.data)

    def test_user_lacks_link_to_admin_index_page_in_index_page(self):
        resp = self.client.post(url_for('login'),
                               data=dict(username=self.user.username,
                                         password=self.user_pw,
                                         token='mocked'),
                               follow_redirects=True)
        admin_link = '<a href="{}">{}</a>'.format(url_for('admin_index'),
                                                  "Admin")
        self.assertNotIn(admin_link, resp.data)

    # WARNING: we are purposely doing something that would not work in
    # production in the _login_user and _login_admin methods. This is done as a
    # reminder to the test developer that the flask_testing.TestCase only uses
    # one request context per method (see
    # https://github.com/freedomofpress/securedrop/issues/1444). By explicitly
    # making a point of this, we hope to avoid the introduction of new tests,
    # that do not truly prove their result because of this disconnect between
    # request context in Flask Testing and production.
    #
    # TODO: either ditch Flask Testing or subclass it as discussed in the
    # aforementioned issue to fix the described problem.
    def _login_admin(self):
        self._ctx.g.user = self.admin

    def _login_user(self):
        self._ctx.g.user = self.user

    def test_admin_logout_redirects_to_index(self):
        self._login_admin()
        resp = self.client.get(url_for('logout'))
        self.assertRedirects(resp, url_for('index'))

    def test_user_logout_redirects_to_index(self):
        self._login_user()
        resp = self.client.get(url_for('logout'))
        self.assertRedirects(resp, url_for('index'))

    def test_admin_index(self):
        self._login_admin()
        resp = self.client.get(url_for('admin_index'))
        self.assert200(resp)
        self.assertIn("Admin Interface", resp.data)

    def test_admin_delete_user(self):
        # Verify journalist is in the database
        self.assertNotEqual(Journalist.query.get(self.user.id), None)

        self._login_admin()
        resp = self.client.post(url_for('admin_delete_user', user_id=self.user.id),
                               follow_redirects=True)

        # Assert correct interface behavior
        self.assert200(resp)
        self.assertIn(escape("Deleted user '{}'".format(self.user.username)),
                      resp.data)
        # Verify journalist is no longer in the database
        self.assertEqual(Journalist.query.get(self.user.id), None)

    def test_admin_deletes_invalid_user_404(self):
        self._login_admin()
        invalid_user_pk = max([user.id for user in Journalist.query.all()]) + 1
        resp = self.client.post(url_for('admin_delete_user',
                                       user_id=invalid_user_pk))
        self.assert404(resp)

    def test_admin_edits_user_password_success_response(self):
        self._login_admin()

        resp = self.client.post(
            url_for('admin_edit_user', user_id=self.user.id),
            data=dict(username=self.user.username, is_admin=False,
                      password='valid', password_again='valid'))

        self.assertIn('Password successfully changed', resp.data)

    def test_user_edits_password_success_reponse(self):
        self._login_user()
        resp = self.client.post(url_for('edit_account'),
                                data=dict(password='valid',
                                          password_again='valid'))
        self.assertIn("Password successfully changed", resp.data)

    def test_admin_edits_user_password_mismatch_warning(self):
        self._login_admin()

        resp = self.client.post(
            url_for('admin_edit_user', user_id=self.user.id),
            data=dict(username=self.user.username, is_admin=False,
                      password='not', password_again='thesame'),
            follow_redirects=True)

        self.assertIn(escape("Passwords didn't match"), resp.data)

    def test_user_edits_password_mismatch_redirect(self):
        self._login_user()
        resp = self.client.post(url_for('edit_account'), data=dict(
            password='not',
            password_again='thesame'))
        self.assertRedirects(resp, url_for('edit_account'))

    def test_admin_add_user_password_mismatch_warning(self):
        self._login_admin()
        resp = self.client.post(url_for('admin_add_user'),
                                data=dict(username='dellsberg',
                                          password='not',
                                          password_again='thesame',
                                          is_admin=False))
        self.assertIn('Passwords didn', resp.data)

    def test_max_password_length(self):
        """Creating a Journalist with a password that is greater than the
        maximum password length should raise an exception"""
        overly_long_password = 'a'*(Journalist.MAX_PASSWORD_LEN + 1)
        with self.assertRaises(InvalidPasswordLength):
            temp_journalist = Journalist(
                    username="My Password is Too Big!",
                    password=overly_long_password)

    def test_admin_edits_user_password_too_long_warning(self):
        self._login_admin()
        overly_long_password = 'a' * (Journalist.MAX_PASSWORD_LEN + 1)

        resp = self.client.post(
            url_for('admin_edit_user', user_id=self.user.id),
            data=dict(username=self.user.username, is_admin=False,
                      password=overly_long_password,
                      password_again=overly_long_password),
            follow_redirects=True)

        self.assertIn('Your password is too long', resp.data)

    def test_user_edits_password_too_long_warning(self):
        self._login_user()
        overly_long_password = 'a' * (Journalist.MAX_PASSWORD_LEN + 1)

        resp = self.client.post(url_for('edit_account'),
                                data=dict(password=overly_long_password,
                                          password_again=overly_long_password),
                                follow_redirects=True)

        self.assertIn('Your password is too long', resp.data)

    def test_admin_add_user_password_too_long_warning(self):
        self._login_admin()

        overly_long_password = 'a' * (Journalist.MAX_PASSWORD_LEN + 1)
        resp = self.client.post(
            url_for('admin_add_user'),
            data=dict(username='dellsberg', password=overly_long_password,
                      password_again=overly_long_password, is_admin=False))

        self.assertIn('password is too long', resp.data)

    def test_admin_edits_user_invalid_username(self):
        """Test expected error message when admin attempts to change a user's
        username to a username that is taken by another user."""
        self._login_admin()
        new_username = self.admin.username

        resp = self.client.post(
            url_for('admin_edit_user', user_id=self.user.id),
            data=dict(username=new_username, is_admin=False,
                      password='', password_again=''))

        self.assertIn('Username {} is already taken'.format(new_username),
                      resp.data)

    def test_admin_resets_user_hotp(self):
        self._login_admin()
        old_hotp = self.user.hotp.secret

        resp = self.client.post(url_for('admin_reset_two_factor_hotp'),
                                data=dict(uid=self.user.id, otp_secret=123456))
        new_hotp = self.user.hotp.secret

        # check that hotp is different
        self.assertNotEqual(old_hotp, new_hotp)
        # Redirect to admin 2FA view
        self.assertRedirects(resp,
            url_for('admin_new_user_two_factor', uid=self.user.id))

    def test_user_resets_hotp(self):
        self._login_user()
        oldHotp = self.user.hotp

        resp = self.client.post(url_for('account_reset_two_factor_hotp'),
                               data=dict(otp_secret=123456))
        newHotp = self.user.hotp

        # check that hotp is different
        self.assertNotEqual(oldHotp, newHotp)
        # should redirect to verification page
        self.assertRedirects(resp, url_for('account_new_two_factor'))

    def test_admin_resets_user_totp(self):
        self._login_admin()
        old_totp = self.user.totp

        resp = self.client.post(
            url_for('admin_reset_two_factor_totp'),
            data=dict(uid=self.user.id))
        new_totp = self.user.totp

        self.assertNotEqual(old_totp, new_totp)

        self.assertRedirects(resp,
            url_for('admin_new_user_two_factor', uid=self.user.id))

    def test_user_resets_totp(self):
        self._login_user()
        oldTotp = self.user.totp

        resp = self.client.post(url_for('account_reset_two_factor_totp'))
        newTotp = self.user.totp

        # check that totp is different
        self.assertNotEqual(oldTotp, newTotp)

        # should redirect to verification page
        self.assertRedirects(resp, url_for('account_new_two_factor'))

    def test_admin_resets_hotp_with_missing_otp_secret_key(self):
        self._login_admin()
        resp = self.client.post(url_for('admin_reset_two_factor_hotp'),
                                data=dict(uid=self.user.id))

        self.assertIn('Change Secret', resp.data)

    def test_admin_new_user_2fa_redirect(self):
        self._login_admin()
        resp = self.client.post(
            url_for('admin_new_user_two_factor', uid=self.user.id),
            data=dict(token='mocked'))
        self.assertRedirects(resp, url_for('admin_index'))

    def test_http_get_on_admin_new_user_two_factor_page(self):
        self._login_admin()
        resp = self.client.get(url_for('admin_new_user_two_factor', uid=self.user.id))
        # any GET req should take a user to the admin_new_user_two_factor page
        self.assertIn('Authenticator', resp.data)

    def test_http_get_on_admin_add_user_page(self):
        self._login_admin()
        resp = self.client.get(url_for('admin_add_user'))
        # any GET req should take a user to the admin_add_user page
        self.assertIn('Add user', resp.data)

    def test_admin_add_user(self):
        self._login_admin()
        max_journalist_pk = max([user.id for user in Journalist.query.all()])

        resp = self.client.post(url_for('admin_add_user'),
                                data=dict(username='dellsberg',
                                          password='pentagonpapers',
                                          password_again='pentagonpapers',
                                          is_admin=False))

        self.assertRedirects(resp, url_for('admin_new_user_two_factor',
                                          uid=max_journalist_pk+1))

    def test_admin_add_user_without_username(self):
        self._login_admin()
        resp = self.client.post(url_for('admin_add_user'),
                                data=dict(username='',
                                          password='pentagonpapers',
                                          password_again='pentagonpapers',
                                          is_admin=False))
        self.assertIn('Missing username', resp.data)

    def test_admin_page_restriction_http_gets(self):
        admin_urls = [url_for('admin_index'), url_for('admin_add_user'),
            url_for('admin_edit_user', user_id=self.user.id)]

        self._login_user()
        for admin_url in admin_urls:
            resp = self.client.get(admin_url)
            self.assertStatus(resp, 302)

    def test_admin_page_restriction_http_posts(self):
        admin_urls = [url_for('admin_reset_two_factor_totp'),
            url_for('admin_reset_two_factor_hotp'),
            url_for('admin_add_user', user_id=self.user.id),
            url_for('admin_new_user_two_factor'),
            url_for('admin_reset_two_factor_totp'),
            url_for('admin_reset_two_factor_hotp'),
            url_for('admin_edit_user', user_id=self.user.id),
            url_for('admin_delete_user', user_id=self.user.id)]
        self._login_user()
        for admin_url in admin_urls:
            resp = self.client.post(admin_url)
            self.assertStatus(resp, 302)

    def test_user_authorization_for_gets(self):
        urls = [url_for('index'), url_for('col', sid='1'),
                url_for('download_single_submission', sid='1', fn='1'),
                url_for('edit_account')]

        for url in urls:
            resp = self.client.get(url)
            self.assertStatus(resp, 302)

    def test_user_authorization_for_posts(self):
        urls = [url_for('add_star', sid='1'), url_for('remove_star', sid='1'),
                url_for('col_process'), url_for('col_delete_single', sid='1'),
                url_for('reply'), url_for('generate_code'), url_for('bulk'),
                url_for('account_new_two_factor'),
                url_for('account_reset_two_factor_totp'),
                url_for('account_reset_two_factor_hotp')]
        for url in urls:
            res = self.client.post(url)
            self.assertStatus(res, 302)

    def test_invalid_user_password_change(self):
        self._login_user()
        res = self.client.post(url_for('edit_account'), data=dict(
            password='not',
            password_again='thesame'))
        self.assertRedirects(res, url_for('edit_account'))

    def test_too_long_user_password_change(self):
        self._login_user()
        overly_long_password = 'a' * (Journalist.MAX_PASSWORD_LEN + 1)

        res = self.client.post(url_for('edit_account'), data=dict(
            password=overly_long_password,
            password_again=overly_long_password),
            follow_redirects=True)

        self.assertIn('Your password is too long', res.data)

    def test_valid_user_password_change(self):
        self._login_user()
        res = self.client.post(url_for('edit_account'), data=dict(
            password='valid',
            password_again='valid'))
        self.assertIn("Password successfully changed", res.data)

    def test_regenerate_totp(self):
        self._login_user()
        oldTotp = self.user.totp

        res = self.client.post(url_for('account_reset_two_factor_totp'))
        newTotp = self.user.totp

        # check that totp is different
        self.assertNotEqual(oldTotp, newTotp)

        # should redirect to verification page
        self.assertRedirects(res, url_for('account_new_two_factor'))

    def test_edit_hotp(self):
        self._login_user()
        oldHotp = self.user.hotp

        res = self.client.post(
            url_for('account_reset_two_factor_hotp'),
            data=dict(otp_secret=123456)
            )
        newHotp = self.user.hotp

        # check that hotp is different
        self.assertNotEqual(oldHotp, newHotp)

        # should redirect to verification page
        self.assertRedirects(res, url_for('account_new_two_factor'))

    def test_change_assignment(self):
        source, _ = utils.db_helper.init_source()
        self._login_user()

        resp = self.client.post(
            url_for('change_assignment', sid=source.filesystem_id),
            data=dict(journalist=self.user.username))

        self.assertRedirects(resp, url_for('index'))
        # Check that source is indeed assigned to self.user.username in the db
        source_assigned = db_session.query(Source).filter(source.filesystem_id
                                                          ==
                                                          source.filesystem_id).one()
        self.assertEqual(self.user.username, source_assigned.journalist.username)


    def test_delete_source_deletes_submissions(self):
        """Verify that when a source is deleted, the submissions that
        correspond to them are also deleted."""
        self._delete_collection_setup()
        journalist.delete_collection(self.source.filesystem_id)

        # Source should be gone
        results = db_session.query(Source).filter(Source.id == self.source.id).all()

    def _delete_collection_setup(self):
        self.source, _ = utils.db_helper.init_source()
        utils.db_helper.submit(self.source, 2)
        utils.db_helper.reply(self.user, self.source, 2)

    def test_delete_collection_updates_db(self):
        """Verify that when a source is deleted, their Source identity
        record, as well as Reply & Submission records associated with
        that record are purged from the database."""
        self._delete_collection_setup()
        journalist.delete_collection(self.source.filesystem_id)
        results = Source.query.filter(Source.id == self.source.id).all()
        self.assertEqual(results, [])
        results = db_session.query(Submission.source_id == self.source.id).all()
        self.assertEqual(results, [])
        results = db_session.query(Reply.source_id == self.source.id).all()
        self.assertEqual(results, [])

    def test_delete_source_deletes_source_key(self):
        """Verify that when a source is deleted, the PGP key that corresponds
        to them is also deleted."""
        self._delete_collection_setup()
        # Source key exists
        source_key = crypto_util.getkey(self.source.filesystem_id)
        self.assertNotEqual(source_key, None)

        journalist.delete_collection(self.source.filesystem_id)

        # Source key no longer exists
        source_key = crypto_util.getkey(self.source.filesystem_id)
        self.assertEqual(source_key, None)

    def test_delete_source_deletes_docs_on_disk(self):
        """Verify that when a source is deleted, the encrypted documents that
        exist on disk is also deleted."""
        self._delete_collection_setup()
        # Encrypted documents exists
        dir_source_docs = os.path.join(config.STORE_DIR, self.source.filesystem_id)
        self.assertTrue(os.path.exists(dir_source_docs))

        job = journalist.delete_collection(self.source.filesystem_id)

        # Wait up to 5s to wait for Redis worker `srm` operation to complete
        utils.async.wait_for_redis_worker(job)

        # Encrypted documents no longer exist
        self.assertFalse(os.path.exists(dir_source_docs))

    def test_download_selected_submissions_from_source(self):
        source, _ = utils.db_helper.init_source()
        submissions = set(utils.db_helper.submit(source, 4))

        selected_submissions = random.sample(submissions, 2)
        selected_fnames = [submission.filename
                           for submission in selected_submissions]

        self._login_user()
        resp = self.client.post(
            '/bulk', data=dict(action='download',
                               sid=source.filesystem_id,
                               doc_names_selected=selected_fnames))
        # The download request was succesful, and the app returned a zipfile
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content_type, 'application/zip')
        self.assertTrue(zipfile.is_zipfile(StringIO(resp.data)))
        # The submissions selected are in the zipfile
        for filename in selected_fnames:
            self.assertTrue(zipfile.ZipFile(StringIO(resp.data)).getinfo(
                os.path.join(source.journalist_filename, filename)))
        # The submissions not selected are absent from the zipfile
        not_selected_submissions = submissions.difference(selected_submissions)
        not_selected_fnames = [submission.filename
                               for submission in not_selected_submissions]
        for filename in not_selected_fnames:
            try:
                zipfile.ZipFile(StringIO(resp.data)).getinfo(
                    os.path.join(source.journalist_filename, filename))
            except KeyError:
                pass
            else:
                self.assertTrue(False)

    def _bulk_download_setup(self):
        """Create a couple sources, make some submissions on their behalf,
        mark some of them as downloaded, and then perform *action* on all
        sources."""
        self.source0, _ = utils.db_helper.init_source()
        self.source1, _ = utils.db_helper.init_source()
        self.submissions0 = set(utils.db_helper.submit(self.source0, 2))
        self.submissions1 = set(utils.db_helper.submit(self.source1, 3))
        self.downloaded0 = random.sample(self.submissions0, 1)
        utils.db_helper.mark_downloaded(*self.downloaded0)
        self.not_downloaded0 = self.submissions0.difference(self.downloaded0)
        self.downloaded1 = random.sample(self.submissions1, 2)
        utils.db_helper.mark_downloaded(*self.downloaded1)
        self.not_downloaded1 = self.submissions1.difference(self.downloaded1)


    def test_download_unread_all_sources(self):
        self._bulk_download_setup()
        self._login_user()
        # Download all unread messages from all sources
        self.resp = self.client.post(
            '/col/process',
            data=dict(action='download-unread',
                      cols_selected=[self.source0.filesystem_id,
                                     self.source1.filesystem_id]))

        # The download request was succesful, and the app returned a zipfile
        self.assertEqual(self.resp.status_code, 200)
        self.assertEqual(self.resp.content_type, 'application/zip')
        self.assertTrue(zipfile.is_zipfile(StringIO(self.resp.data)))
        # All the not dowloaded submissions are in the zipfile
        for submission in self.not_downloaded0.union(self.not_downloaded1):
            self.assertTrue(
                zipfile.ZipFile(StringIO(self.resp.data)).getinfo(
                    os.path.join('unread', submission.filename))
                )
        # All the downloaded submissions are absent from the zipfile
        for submission in self.downloaded0 + self.downloaded1:
            try:
                zipfile.ZipFile(StringIO(self.resp.data)).getinfo(
                    os.path.join('unread', submission.filename))
            except KeyError:
                pass
            else:
                self.assertTrue(False)

    def test_download_all_selected_sources(self):
        self._bulk_download_setup()
        self._login_user()
        # Dowload all messages from self.source1
        self.resp = self.client.post(
            '/col/process',
            data=dict(action='download-all',
                      cols_selected=[self.source1.filesystem_id]))

        # The download request was succesful, and the app returned a zipfile
        self.assertEqual(self.resp.status_code, 200)
        self.assertEqual(self.resp.content_type, 'application/zip')
        self.assertTrue(zipfile.is_zipfile(StringIO(self.resp.data)))
        # All messages from self.source1 are in the zipfile
        for submission in self.submissions1:
            self.assertTrue(
                zipfile.ZipFile(StringIO(self.resp.data)).getinfo(
                    os.path.join('all', submission.filename))
                )
        # All messages from self.source2 are absent from the zipfile
        for submission in self.submissions0:
            try:
                zipfile.ZipFile(StringIO(self.resp.data)).getinfo(
                    os.path.join('all', submission.filename))
            except KeyError:
                pass
            else:
                self.assertTrue(False)

    def test_add_star_redirects_to_index(self):
        source, _ = utils.db_helper.init_source()
        self._login_user()
        resp = self.client.post(url_for('add_star', sid=source.filesystem_id))
        self.assertRedirects(resp, url_for('index'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
