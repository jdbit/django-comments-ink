from __future__ import unicode_literals

import importlib
import json
import random
import re
import string
from datetime import datetime
from typing import List
from unittest.mock import Mock, patch

import django_comments
import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.contenttypes.models import ContentType
from django.contrib.sites.models import Site
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import JsonResponse
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django_comments.views.comments import CommentPostBadRequest
from django_comments_ink import signals, signed, views
from django_comments_ink.conf import settings
from django_comments_ink.models import InkComment
from django_comments_ink.tests.models import Article, Diary

request_factory = RequestFactory()


def post_article_comment(data, article, auth_user=None):
    request = request_factory.post(
        reverse(
            "article-detail",
            kwargs={
                "year": article.publish.year,
                "month": article.publish.month,
                "day": article.publish.day,
                "slug": article.slug,
            },
        ),
        data=data,
        follow=True,
    )
    if auth_user:
        request.user = auth_user
    else:
        request.user = AnonymousUser()
    request._dont_enforce_csrf_checks = True
    return views.post(request)


def post_diary_comment(data, diary_entry, auth_user=None):
    request = request_factory.post(
        reverse(
            "diary-detail",
            kwargs={
                "year": diary_entry.publish.year,
                "month": diary_entry.publish.month,
                "day": diary_entry.publish.day,
            },
        ),
        data=data,
        follow=True,
    )
    if auth_user:
        request.user = auth_user
    else:
        request.user = AnonymousUser()
    request._dont_enforce_csrf_checks = True
    return views.post(request)
    # return comments.post_comment(request)


def confirm_comment_url(key, follow=True):
    request = request_factory.get(
        reverse("comments-ink-confirm", kwargs={"key": key}), follow=follow
    )
    request.user = AnonymousUser()
    return views.confirm(request, key)


app_model_options_mock = {"tests.article": {"who_can_post": "users"}}


class OnCommentWasPostedTestCase(TestCase):
    def setUp(self):
        self.patcher = patch("django_comments_ink.views.utils.send_mail")
        self.mock_mailer = self.patcher.start()
        self.article = Article.objects.create(
            title="October", slug="october", body="What I did on October..."
        )
        self.form = django_comments.get_form()(self.article)
        self.user = AnonymousUser()

    def tearDown(self):
        self.patcher.stop()

    def post_valid_data(self, auth_user=None, response_code=302):
        data = {
            "name": "Bob",
            "email": "bob@example.com",
            "followup": True,
            "reply_to": 0,
            "level": 1,
            "order": 1,
            "comment": "Es war einmal eine kleine...",
        }
        data.update(self.form.initial)
        response = post_article_comment(data, self.article, auth_user)
        self.assertEqual(response.status_code, response_code)
        if response.status_code == 302:
            self.assertTrue(response.url.startswith("/comments/posted/?c="))

    def post_invalid_data(
        self, auth_user=None, response_code=302, remove_fields: List[str] = []
    ):
        data = {
            "name": "Bob",
            "email": "bob@example.com",
            "followup": True,
            "reply_to": 0,
            "level": 1,
            "order": 1,
            "comment": "Es war einmal eine kleine...",
        }
        data.update(self.form.initial)
        if len(remove_fields):
            for field_name in remove_fields:
                data.pop(field_name)
        response = post_article_comment(data, self.article, auth_user)
        self.assertEqual(response.status_code, response_code)
        if response.status_code == 302:
            self.assertTrue(response.url.startswith("/comments/posted/?c="))

    def test_post_as_authenticated_user(self):
        self.user = User.objects.create_user("bob", "bob@example.com", "pwd")
        self.assertTrue(self.mock_mailer.call_count == 0)
        self.post_valid_data(auth_user=self.user)
        # no confirmation email sent as user is authenticated
        self.assertTrue(self.mock_mailer.call_count == 0)

    def test_post_as_authenticated_user_without_name_nor_email(self):
        data = {
            "followup": True,
            "reply_to": 0,
            "level": 1,
            "order": 1,
            "comment": "Es war einmal eine kleine...",
        }
        self.user = User.objects.create_user("bob", "bob@example.com", "pwd")
        self.assertTrue(self.mock_mailer.call_count == 0)
        self.post_invalid_data(
            auth_user=self.user, remove_fields=["name", "email"]
        )
        # no confirmation email sent as user is authenticated via self.user.
        self.assertTrue(self.mock_mailer.call_count == 0)

    # def test_post_comment_form_without_content_type(self):
    #     self.user = User.objects.create_user("bob", "bob@example.com", "pwd")
    #     self.assertTrue(self.mock_mailer.call_count == 0)
    #     self.post_invalid_data(
    #         auth_user=self.user,
    #         response_code=400,
    #         remove_fields=['content_type']
    #     )

    def test_confirmation_email_is_sent(self):
        self.assertTrue(self.mock_mailer.call_count == 0)
        self.post_valid_data()
        self.assertTrue(self.mock_mailer.call_count == 1)

    @patch.multiple(
        "django_comments_ink.conf.settings",
        COMMENTS_INK_APP_MODEL_OPTIONS=app_model_options_mock,
    )
    def test_post_as_visitor_when_only_users_can_post(self):
        self.assertTrue(self.mock_mailer.call_count == 0)
        self.post_valid_data(response_code=400)
        self.assertTrue(self.mock_mailer.call_count == 0)


class ConfirmCommentTestCase(TestCase):
    def setUp(self):
        patcher = patch("django_comments_ink.views.utils.send_mail")
        self.mock_mailer = patcher.start()
        # Create random string so that it's harder for zlib to compress
        content = "".join(random.choice(string.printable) for _ in range(6096))
        self.article = Article.objects.create(
            title="September",
            slug="september",
            body="In September..." + content,
        )
        self.form = django_comments.get_form()(self.article)
        data = {
            "name": "Bob",
            "email": "bob@example.com",
            "followup": True,
            "reply_to": 0,
            "level": 1,
            "order": 1,
            "comment": "Es war einmal iene kleine...",
        }
        data.update(self.form.initial)
        response = post_article_comment(data, self.article)
        self.assertTrue(self.mock_mailer.call_count == 1)
        self.key = str(
            re.search(
                r"http://.+/confirm/(?P<key>[\S]+)/",
                self.mock_mailer.call_args[0][1],
            ).group("key")
        )
        self.addCleanup(patcher.stop)

    def test_confirm_url_is_short_enough(self):
        # Tests that the length of the confirm url's length isn't
        # dependent on the article length.
        l = len(reverse("comments-ink-confirm", kwargs={"key": self.key}))
        # print("\nXXXXXXXXXXX:", l)
        self.assertLessEqual(l, 4096, "Urls can only be a max of 4096")

    def test_400_on_bad_signature(self):
        response = confirm_comment_url(self.key[:-1])
        self.assertEqual(response.status_code, 400)

    def test_consecutive_confirmation_url_visits_doesnt_fail(self):
        # test that consecutives visits to the same confirmation URL produce
        # an Http 404 code, as the comment has already been verified in the
        # first visit
        response = confirm_comment_url(self.key)
        self.assertEqual(response.status_code, 302)
        confirm_comment_url(self.key)
        self.assertEqual(response.status_code, 302)

    def test_signal_receiver_may_discard_the_comment(self):
        # test that receivers of signal confirmation_received may return False
        # and thus rendering a template_discarded output
        def on_signal(sender, comment, request, **kwargs):
            return False

        self.assertEqual(self.mock_mailer.call_count, 1)  # sent during setUp
        signals.confirmation_received.connect(on_signal)
        response = confirm_comment_url(self.key)
        # mailing avoided by on_signal:
        self.assertEqual(self.mock_mailer.call_count, 1)
        self.assertTrue(response.content.find(b"Comment discarded") > -1)

    def test_comment_is_created_and_view_redirect(self):
        # testing that visiting a correct confirmation URL creates a InkComment
        # and redirects to the article detail page
        Site.objects.get_current().domain = "testserver"  # django bug #7743
        response = confirm_comment_url(self.key, follow=False)
        data = signed.loads(self.key, extra_key=settings.COMMENTS_INK_SALT)
        try:
            comment = InkComment.objects.get(
                content_type=data["content_type"],
                user_name=data["user_name"],
                user_email=data["user_email"],
                submit_date=data["submit_date"],
            )
        except:
            comment = None
        self.assertTrue(comment is not None)
        self.assertEqual(response.url, comment.get_absolute_url())

    def test_notify_comment_followers(self):
        # send a couple of comments to the article with followup=True and check
        # that when the second comment is confirmed a followup notification
        # email is sent to the user who sent the first comment
        self.assertEqual(self.mock_mailer.call_count, 1)
        confirm_comment_url(self.key)
        # no comment followers yet:
        self.assertEqual(self.mock_mailer.call_count, 1)
        # send 2nd comment
        self.form = django_comments.get_form()(self.article)
        data = {
            "name": "Alice",
            "email": "alice@example.com",
            "followup": True,
            "reply_to": 0,
            "level": 1,
            "order": 1,
            "comment": "Es war einmal eine kleine...",
        }
        data.update(self.form.initial)
        response = post_article_comment(data, article=self.article)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/comments/posted/?c="))
        self.assertEqual(self.mock_mailer.call_count, 2)
        self.key = re.search(
            r"http://.+/confirm/(?P<key>[\S]+)/",
            self.mock_mailer.call_args[0][1],
        ).group("key")
        confirm_comment_url(self.key)
        self.assertEqual(self.mock_mailer.call_count, 3)
        self.assertTrue(self.mock_mailer.call_args[0][3] == ["bob@example.com"])
        self.assertTrue(
            self.mock_mailer.call_args[0][1].find(
                "There is a new comment following up yours."
            )
            > -1
        )

    def test_notify_followers_dupes(self):
        # first of all confirm Bob's comment otherwise it doesn't reach DB
        confirm_comment_url(self.key)
        # then put in play pull-request-15's assert...
        # https://github.com/danirus/django-comments-xtd/pull/15
        diary = Diary.objects.create(body="Lorem ipsum", allow_comments=True)
        self.assertEqual(diary.pk, self.article.pk)

        self.form = django_comments.get_form()(diary)
        data = {
            "name": "Charlie",
            "email": "charlie@example.com",
            "followup": True,
            "reply_to": 0,
            "level": 1,
            "order": 1,
            "comment": "Es war einmal eine kleine...",
        }
        data.update(self.form.initial)
        response = post_diary_comment(data, diary_entry=diary)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/comments/posted/?c="))
        self.key = str(
            re.search(
                r"http://.+/confirm/(?P<key>[\S]+)/",
                self.mock_mailer.call_args[0][1],
            ).group("key")
        )
        # 1) confirmation for Bob (sent in `setUp()`)
        # 2) confirmation for Charlie
        self.assertEqual(self.mock_mailer.call_count, 2)
        response = confirm_comment_url(self.key)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/comments/cr/"))
        self.assertEqual(self.mock_mailer.call_count, 2)

        self.form = django_comments.get_form()(self.article)
        data = {
            "name": "Alice",
            "email": "alice@example.com",
            "followup": True,
            "reply_to": 0,
            "level": 1,
            "order": 1,
            "comment": "Es war einmal iene kleine...",
        }
        data.update(self.form.initial)
        response = post_article_comment(data, article=self.article)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/comments/posted/?c="))
        self.assertEqual(self.mock_mailer.call_count, 3)
        self.key = re.search(
            r"http://.+/confirm/(?P<key>[\S]+)/",
            self.mock_mailer.call_args[0][1],
        ).group("key")
        confirm_comment_url(self.key)
        self.assertEqual(self.mock_mailer.call_count, 4)
        self.assertTrue(self.mock_mailer.call_args[0][3] == ["bob@example.com"])
        self.assertTrue(
            self.mock_mailer.call_args[0][1].find(
                "There is a new comment following up yours."
            )
            > -1
        )

    def test_no_notification_for_same_user_email(self):
        # test that a follow-up user_email don't get a notification when
        # sending another email to the thread
        self.assertEqual(self.mock_mailer.call_count, 1)
        confirm_comment_url(self.key)  # confirm Bob's comment
        # no comment followers yet:
        self.assertEqual(self.mock_mailer.call_count, 1)
        # send Bob's 2nd comment
        self.form = django_comments.get_form()(self.article)
        data = {
            "name": "Alice",
            "email": "bob@example.com",
            "followup": True,
            "reply_to": 0,
            "level": 1,
            "order": 1,
            "comment": "Bob's comment he shouldn't get notified about",
        }
        data.update(self.form.initial)
        response = post_article_comment(data, self.article)
        self.assertEqual(self.mock_mailer.call_count, 2)
        self.key = re.search(
            r"http://.+/confirm/(?P<key>[\S]+)/",
            self.mock_mailer.call_args[0][1],
        ).group("key")
        confirm_comment_url(self.key)
        self.assertEqual(self.mock_mailer.call_count, 2)


class ReplyNoCommentTestCase(TestCase):
    def test_reply_non_existing_comment_raises_404(self):
        response = self.client.get(
            reverse("comments-ink-reply", kwargs={"cid": 1})
        )
        self.assertContains(response, "404", status_code=404)


class ReplyCommentTestCase(TestCase):
    def setUp(self):
        article = Article.objects.create(
            title="September",
            slug="september",
            body="What I did on September...",
        )
        article_ct = ContentType.objects.get(app_label="tests", model="article")
        site = Site.objects.get(pk=1)

        # post Comment 1 to article, level 0
        InkComment.objects.create(
            content_type=article_ct,
            object_pk=article.id,
            content_object=article,
            site=site,
            comment="comment 1 to article",
            submit_date=datetime.now(),
        )

        # post Comment 2 to article, level 1
        InkComment.objects.create(
            content_type=article_ct,
            object_pk=article.id,
            content_object=article,
            site=site,
            comment="comment 1 to comment 1",
            submit_date=datetime.now(),
            parent_id=1,
        )

        # post Comment 3 to article, level 2 (max according to test settings)
        InkComment.objects.create(
            content_type=article_ct,
            object_pk=article.id,
            content_object=article,
            site=site,
            comment="comment 1 to comment 1",
            submit_date=datetime.now(),
            parent_id=2,
        )

    def test_reply_view(self):
        response = self.client.get(
            reverse("comments-ink-reply", kwargs={"cid": 3})
        )
        self.assertEqual(response.status_code, 200)

    @patch.multiple(
        "django_comments_ink.conf.settings", COMMENTS_INK_MAX_THREAD_LEVEL=2
    )
    def test_not_allow_threaded_reply_raises_403(self):
        response = self.client.get(
            reverse("comments-ink-reply", kwargs={"cid": 3})
        )
        self.assertEqual(response.status_code, 403)

    @patch.multiple(
        "django_comments_ink.conf.settings",
        COMMENTS_INK_APP_MODEL_OPTIONS=app_model_options_mock,
    )
    def test_reply_as_visitor_when_only_users_can_post(self):
        response = self.client.get(
            reverse("comments-ink-reply", kwargs={"cid": 1})
        )
        self.assertEqual(response.status_code, 302)  # Redirect to login.
        self.assertTrue(response.url.startswith(settings.LOGIN_URL))


class MuteFollowUpTestCase(TestCase):
    def setUp(self):
        # Creates an article and send two comments to the article with
        # follow-up notifications. First comment doesn't have to send any
        #  notification.
        # Second comment has to send one notification (to Bob).
        patcher = patch("django_comments_ink.views.utils.send_mail")
        self.mock_mailer = patcher.start()
        self.article = Article.objects.create(
            title="September", slug="september", body="John's September"
        )
        self.form = django_comments.get_form()(self.article)

        # Bob sends 1st comment to the article with follow-up
        data = {
            "name": "Bob",
            "email": "bob@example.com",
            "followup": True,
            "reply_to": 0,
            "level": 1,
            "order": 1,
            "comment": "Nice September you had...",
        }
        data.update(self.form.initial)
        response = post_article_comment(data, self.article)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/comments/posted/?c="))
        self.assertTrue(self.mock_mailer.call_count == 1)
        bobkey = str(
            re.search(
                r"http://.+/confirm/(?P<key>[\S]+)/",
                self.mock_mailer.call_args[0][1],
            ).group("key")
        )
        confirm_comment_url(bobkey)  # confirm Bob's comment

        # Alice sends 2nd comment to the article with follow-up
        data = {
            "name": "Alice",
            "email": "alice@example.com",
            "followup": True,
            "reply_to": 1,
            "level": 1,
            "order": 1,
            "comment": "Yeah, great photos",
        }
        data.update(self.form.initial)
        response = post_article_comment(data, self.article)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/comments/posted/?c="))
        self.assertTrue(self.mock_mailer.call_count == 2)
        alicekey = str(
            re.search(
                r"http://.+/confirm/(?P<key>[\S]+)/",
                self.mock_mailer.call_args[0][1],
            ).group("key")
        )
        confirm_comment_url(alicekey)  # confirm Alice's comment

        # Bob receives a follow-up notification
        self.assertTrue(self.mock_mailer.call_count == 3)
        self.bobs_mutekey = str(
            re.search(
                r"http://.+/mute/(?P<key>[\S]+)/",
                self.mock_mailer.call_args[0][1],
            ).group("key")
        )
        self.addCleanup(patcher.stop)

    def get_mute_followup_url(self, key):
        request = request_factory.get(
            reverse("comments-ink-mute", kwargs={"key": key}), follow=True
        )
        request.user = AnonymousUser()
        response = views.mute(request, key)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.find(b"Comment thread muted") > -1)
        return response

    def test_mute_followup_notifications(self):
        # Bob's receive a notification and click on the mute link to
        # avoid additional comment messages on the same article.
        self.get_mute_followup_url(self.bobs_mutekey)
        # Alice sends 3rd comment to the article with follow-up
        data = {
            "name": "Alice",
            "email": "alice@example.com",
            "followup": True,
            "reply_to": 2,
            "level": 1,
            "order": 1,
            "comment": "And look at this and that...",
        }
        data.update(self.form.initial)
        response = post_article_comment(data, self.article)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/comments/posted/?c="))
        # Alice confirms her comment...
        self.assertTrue(self.mock_mailer.call_count == 4)
        alicekey = str(
            re.search(
                r"http://.+/confirm/(?P<key>[\S]+)/",
                self.mock_mailer.call_args[0][1],
            ).group("key")
        )
        confirm_comment_url(alicekey)  # confirm Alice's comment
        # Alice confirmed her comment, but this time Bob won't receive any
        # notification, neither do Alice being the sender
        self.assertTrue(self.mock_mailer.call_count == 4)


class HTMLDisabledMailTestCase(TestCase):
    def setUp(self):
        # Create an article and send a comment. Test method will chech headers
        # to see wheter messages has multiparts or not.
        patcher = patch("django_comments_ink.views.utils.send_mail")
        self.mock_mailer = patcher.start()
        self.article = Article.objects.create(
            title="September", slug="september", body="John's September"
        )
        self.form = django_comments.get_form()(self.article)

        # Bob sends 1st comment to the article with follow-up
        self.data = {
            "name": "Bob",
            "email": "bob@example.com",
            "followup": True,
            "reply_to": 0,
            "level": 1,
            "order": 1,
            "comment": "Nice September you had...",
        }
        self.data.update(self.form.initial)

    @patch.multiple(
        "django_comments_ink.conf.settings", COMMENTS_INK_SEND_HTML_EMAIL=False
    )
    def test_mail_does_not_contain_html_part(self):
        with patch.multiple(
            "django_comments_ink.conf.settings",
            COMMENTS_INK_SEND_HTML_EMAIL=False,
        ):
            response = post_article_comment(self.data, self.article)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.url.startswith("/comments/posted/?c="))
            self.assertTrue(self.mock_mailer.call_count == 1)
            self.assertTrue(self.mock_mailer.call_args[1]["html"] is None)

    def test_mail_does_contain_html_part(self):
        response = post_article_comment(self.data, self.article)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/comments/posted/?c="))
        self.assertTrue(self.mock_mailer.call_count == 1)
        self.assertTrue(self.mock_mailer.call_args[1]["html"] is not None)


# ---------------------------------------------------------------------
# Test module level `_*_tmpl` variables. Verify that they include
# the them (settings.COMMENTS_INK_THEME_DIR) in the path when that
# setting is provided.


def test_template_path_includes_theme(monkeypatch):
    monkeypatch.setattr(
        views.settings, "COMMENTS_INK_THEME_DIR", "avatar_in_header"
    )
    importlib.reload(views)
    assert views.theme_dir == "themes/avatar_in_header"
    assert views.theme_dir_exists == True
    monkeypatch.setattr(views.settings, "COMMENTS_INK_THEME_DIR", "")
    importlib.reload(views)


def test_template_path_does_not_include_theme(monkeypatch):
    assert views.theme_dir == ""
    assert views.theme_dir_exists == False


# ---------------------------------------------------------------------
# Test 'post' via accessing the exposed functionality, to
# later replace the implementation with class-based views.


def test_post_view_requires_method_to_be_POST(rf):
    # GET should not work.
    request = rf.get(reverse("comments-ink-post"))
    request._dont_enforce_csrf_checks = True
    response = views.post(request)
    assert response.status_code == 405

    # PUT should not work.
    request = rf.put(reverse("comments-ink-post"))
    request._dont_enforce_csrf_checks = True
    response = views.post(request)
    assert response.status_code == 405

    # PATCH should not work.
    request = rf.patch(reverse("comments-ink-post"))
    request._dont_enforce_csrf_checks = True
    response = views.post(request)
    assert response.status_code == 405


def mocked_post_js(*args, **kwargs):
    return JsonResponse({"post_js_called": True}, status=200)


def test_XMLHttpRequest_post_view_handles_to_post_js(rf, monkeypatch):
    request = rf.post(
        reverse(
            "comments-ink-post",
        ),
        data={},
    )
    request._dont_enforce_csrf_checks = True
    request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
    monkeypatch.setattr(views, "post_js", mocked_post_js)
    response = views.post(request)
    assert response.status_code == 200
    assert json.loads(response.content) == {"post_js_called": True}


# ---------------------------------------------------------------------
class MockedPostBadRequest(CommentPostBadRequest):
    def __init__(self, why):
        self.why = why
        super().__init__(why)


def prepare_comment_form_data(an_article):
    form = django_comments.get_form()(an_article)
    data = {
        "name": "Joe",
        "email": "joe@example.com",
        "followup": True,
        "reply_to": 0,
        "level": 1,
        "order": 1,
        "comment": "Es war einmal eine kleine...",
    }
    data.update(form.initial)
    return data


def prepare_request_to_post_form(
    monkeypatch, rf, an_article, an_user, remove_fields=[], add_fields=[]
):
    monkeypatch.setattr(views, "CommentPostBadRequest", MockedPostBadRequest)
    data = prepare_comment_form_data(an_article)

    # Remove fields listed in remove_fields.
    for field_name in remove_fields:
        data.pop(field_name)

    # Add fields listed in add_fields as {"name": <name>, "value": <value>}
    for field in add_fields:
        data[field["name"]] = field["value"]

    article_url = reverse(
        "article-detail",
        kwargs={
            "year": an_article.publish.year,
            "month": an_article.publish.month,
            "day": an_article.publish.day,
            "slug": an_article.slug,
        },
    )
    request = rf.post(article_url, data=data, follow=True)
    request.user = an_user
    request._dont_enforce_csrf_checks = True
    return request


@pytest.mark.django_db
def test_post_comment_form_without_content_type(
    monkeypatch, rf, an_article, an_user
):
    request = prepare_request_to_post_form(
        monkeypatch, rf, an_article, an_user, remove_fields=["content_type"]
    )
    response = views.post(request)
    assert response.status_code == 400
    assert response.why == "Missing content_type or object_pk field."


@pytest.mark.django_db
def test_post_comment_form_without_object_pk(
    monkeypatch, rf, an_article, an_user
):
    request = prepare_request_to_post_form(
        monkeypatch, rf, an_article, an_user, remove_fields=["object_pk"]
    )
    response = views.post(request)
    assert response.status_code == 400
    assert response.why == "Missing content_type or object_pk field."


def mock_get_model(etr):
    def _mocked_function(*args, **kwargs):
        raise etr("Something went wrong")

    return _mocked_function


@pytest.mark.django_db
@pytest.mark.parametrize(
    "monkeypatch, rf, an_article, an_user, exc, message",
    [
        (
            "monkeypatch",
            "rf",
            "an_article",
            "an_user",
            LookupError,
            "Invalid content_type value",
        ),
        (
            "monkeypatch",
            "rf",
            "an_article",
            "an_user",
            TypeError,
            "Invalid content_type value",
        ),
        (
            "monkeypatch",
            "rf",
            "an_article",
            "an_user",
            AttributeError,
            "The given content-type",
        ),
        (
            "monkeypatch",
            "rf",
            "an_article",
            "an_user",
            ObjectDoesNotExist,
            "No object matching content-type",
        ),
        (
            "monkeypatch",
            "rf",
            "an_article",
            "an_user",
            ValueError,
            "Attempting to get content-type",
        ),
        (
            "monkeypatch",
            "rf",
            "an_article",
            "an_user",
            ValidationError,
            "Attempting to get content-type",
        ),
    ],
    indirect=["monkeypatch", "rf", "an_article", "an_user"],
)
def test_post_comment_form_raises_an_error(
    monkeypatch, rf, an_article, an_user, exc, message
):
    monkeypatch.setattr(views.apps, "get_model", mock_get_model(exc))
    request = prepare_request_to_post_form(monkeypatch, rf, an_article, an_user)
    response = views.post(request)
    assert response.status_code == 400
    assert response.why.startswith(message)


# ---------------------------------------------------------------------


def get_form_mocked(has_errors=False):
    class MockedForm:
        def __init__(self, target, data=None):
            self.has_errors = has_errors
            self.target = target
            self.data = data

        def security_errors(self):
            return self.has_errors

    return MockedForm


@pytest.mark.django_db
def test_post_comment_form_with_security_errors(
    monkeypatch, rf, an_article, an_user
):
    monkeypatch.setattr(
        views, "get_form", lambda: get_form_mocked(has_errors=True)
    )
    request = prepare_request_to_post_form(monkeypatch, rf, an_article, an_user)
    response = views.post(request)
    assert response.status_code == 400
    assert response.why.startswith("The comment form failed security ")


# ---------------------------------------------------------------------
# Check the preview templates in the following conditions:
#  1st: when the theme_dir is left blank.
#  2nd: when the theme_dir is been given in the settings.
#  3rd: when the form had errors (remove the 'comment' field and add it empty.)


@pytest.mark.django_db
def test_post_comment_form_in_preview_without_theme_dir(
    monkeypatch, rf, an_article, an_user
):
    monkeypatch.setattr(views, "render", lambda x, tmpl_list, y: tmpl_list)
    request = prepare_request_to_post_form(
        monkeypatch,
        rf,
        an_article,
        an_user,
        add_fields=[
            {"name": "preview", "value": 1},
        ],
    )
    template_list = views.post(request)
    assert template_list == [
        "comments/tests/article/preview.html",
        "comments/tests/preview.html",
        "comments/preview.html",
    ]


@pytest.mark.django_db
def test_post_comment_form_in_preview_with_theme_dir(
    monkeypatch, rf, an_article, an_user
):
    monkeypatch.setattr(
        views.settings, "COMMENTS_INK_THEME_DIR", "feedback_in_header"
    )
    importlib.reload(views)

    monkeypatch.setattr(views, "render", lambda x, tmpl_list, y: tmpl_list)
    request = prepare_request_to_post_form(
        monkeypatch,
        rf,
        an_article,
        an_user,
        add_fields=[
            {"name": "preview", "value": 1},
        ],
    )
    template_list = views.post(request)
    assert template_list == [
        "comments/themes/feedback_in_header/tests/article/preview.html",
        "comments/themes/feedback_in_header/tests/preview.html",
        "comments/themes/feedback_in_header/preview.html",
        "comments/tests/article/preview.html",
        "comments/tests/preview.html",
        "comments/preview.html",
    ]
    # Revert theme.
    monkeypatch.setattr(views.settings, "COMMENTS_INK_THEME_DIR", "")
    importlib.reload(views)


@pytest.mark.django_db
def test_post_comment_form_with_an_empty_comment_field(
    monkeypatch, rf, an_article, an_user
):
    monkeypatch.setattr(views, "render", lambda x, tmpl_list, y: tmpl_list)
    request = prepare_request_to_post_form(
        monkeypatch,
        rf,
        an_article,
        an_user,
        remove_fields=["comment"],
        add_fields=[
            {"name": "comment", "value": ""},
        ],
    )
    template_list = views.post(request)
    assert template_list == [
        "comments/tests/article/preview.html",
        "comments/tests/preview.html",
        "comments/preview.html",
    ]


# ----------------------------------------------------------------------
# Test that during the post of the comment form the
# signal 'comment_will_be_posted' has been called.
#  1. Check that when the signal returns False, a HTTP 400 is returned.
#  2. Check that when the signal returns True, the comment is posted.


@pytest.mark.django_db
def test_post_comment_form__comment_will_be_posted__returns_400(
    monkeypatch, rf, an_article, an_user
):
    def mock_send(*args, **kwargs):
        class Receiver:
            def __init__(self):
                self.__name__ = "mocked receiver"

        return [
            (Receiver(), False),
        ]

    monkeypatch.setattr(views.comment_will_be_posted, "send", mock_send)
    request = prepare_request_to_post_form(monkeypatch, rf, an_article, an_user)
    response = views.post(request)
    assert response.status_code == 400
    assert response.why.startswith("comment_will_be_posted receiver")


@pytest.mark.django_db
def test_post_comment_form__comment_will_be_posted__returns_302(
    monkeypatch, rf, an_article, an_user
):
    def mock_send(*args, **kwargs):
        return [
            (None, True),
        ]

    monkeypatch.setattr(views.comment_will_be_posted, "send", mock_send)
    request = prepare_request_to_post_form(monkeypatch, rf, an_article, an_user)
    response = views.post(request)
    assert response.status_code == 302
    assert response.url == "/comments/posted/?c=1"


# ----------------------------------------------------------------------
# Test that 'comment_was_posted' signal has been sent.
# Follow the same approach as here above, but check
# that method 'send' is called.


@pytest.mark.django_db
def test_post_comment_form__comment_was_posted__signal_sent(
    monkeypatch, rf, an_article, an_user
):
    mock_send = Mock()
    monkeypatch.setattr(views.comment_was_posted, "send", mock_send)
    request = prepare_request_to_post_form(monkeypatch, rf, an_article, an_user)
    views.post(request)
    assert mock_send.called


# ---------------------------------------------------------------------
@pytest.mark.django_db
def test_post_comment_form_has_cpage_qs_param(
    monkeypatch, rf, an_article, an_user
):
    request = prepare_request_to_post_form(
        monkeypatch,
        rf,
        an_article,
        an_user,
        add_fields=[{"name": "cpage", "value": 2}],
    )
    response = views.post(request)
    assert response.status_code == 302
    assert response.url == "/comments/posted/?c=1&cpage=2"


# ---------------------------------------------------------------------
def prepare_js_request_to_post_form(
    rf, an_article, an_user, remove_fields=[], add_fields=[]
):
    data = prepare_comment_form_data(an_article)

    # Remove fields listed in remove_fields.
    for field_name in remove_fields:
        data.pop(field_name)

    # Add fields listed in add_fields as {"name": <name>, "value": <value>}
    for field in add_fields:
        data[field["name"]] = field["value"]

    article_url = reverse(
        "article-detail",
        kwargs={
            "year": an_article.publish.year,
            "month": an_article.publish.month,
            "day": an_article.publish.day,
            "slug": an_article.slug,
        },
    )
    request = rf.post(article_url, data=data, follow=True)
    request.user = an_user
    request._dont_enforce_csrf_checks = True
    request.META["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
    return request


@pytest.mark.django_db
def test_post_js_comment_form_missing_name_and_email(rf, an_article, an_user):
    request = prepare_js_request_to_post_form(
        rf, an_article, an_user, remove_fields=["name", "email"]
    )
    response = views.post(request)
    assert response.status_code == 201
    result = json.loads(response.content)
    assert result["html"].find("Your comment has been already published.") > -1
    comment = InkComment.objects.get(pk=1)
    assert comment.user == an_user


@pytest.mark.django_db
@pytest.mark.parametrize(
    "rf, an_article, an_user, remove_field",
    [
        ("rf", "an_article", "an_user", "content_type"),
        ("rf", "an_article", "an_user", "object_pk"),
    ],
    indirect=["rf", "an_article", "an_user"],
)
def test_post_js_comment_form_missing_content_type_or_object_pk(
    rf, an_article, an_user, remove_field
):
    request = prepare_js_request_to_post_form(
        rf, an_article, an_user, remove_fields=[remove_field]
    )
    response = views.post(request)
    assert response.status_code == 400
    result = json.loads(response.content)
    assert result["html"].find("Missing content_type or object_pk field.") > -1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "monkeypatch, rf, an_article, an_user, exc, message",
    [
        (
            "monkeypatch",
            "rf",
            "an_article",
            "an_user",
            LookupError,
            "Invalid content_type value",
        ),
        (
            "monkeypatch",
            "rf",
            "an_article",
            "an_user",
            TypeError,
            "Invalid content_type value",
        ),
        (
            "monkeypatch",
            "rf",
            "an_article",
            "an_user",
            AttributeError,
            "The given content-type",
        ),
        (
            "monkeypatch",
            "rf",
            "an_article",
            "an_user",
            ObjectDoesNotExist,
            "No object matching content-type",
        ),
        (
            "monkeypatch",
            "rf",
            "an_article",
            "an_user",
            ValueError,
            "Attempting to get content-type",
        ),
        (
            "monkeypatch",
            "rf",
            "an_article",
            "an_user",
            ValidationError,
            "Attempting to get content-type",
        ),
    ],
    indirect=["monkeypatch", "rf", "an_article", "an_user"],
)
def test_post_js_comment_form_returns_400(
    monkeypatch, rf, an_article, an_user, exc, message
):
    monkeypatch.setattr(views.apps, "get_model", mock_get_model(exc))
    request = prepare_js_request_to_post_form(rf, an_article, an_user)
    response = views.post(request)
    assert response.status_code == 400
    result = json.loads(response.content)
    assert result["html"].find(message) > -1


@pytest.mark.django_db
def test_post_js_comment_form_with_security_errors(
    monkeypatch, rf, an_article, an_user
):
    monkeypatch.setattr(
        views, "get_form", lambda: get_form_mocked(has_errors=True)
    )
    request = prepare_js_request_to_post_form(rf, an_article, an_user)
    response = views.post(request)
    assert response.status_code == 400
    result = json.loads(response.content)
    assert result["html"].find("The comment form failed security") > -1


# ---------------------------------------------------------------------
# Do a JS Post and check the preview templates in the following
# conditions:
#  1st: when the reply_to field is 0, and the theme_dir is left blank.
#  2nd: when the reply_to field is 1, and the theme_dir is left blank.
#  3nd: when the reply_to is 0 and theme_dir is given in the settings.
#  4th: when the reply_to is 1 and theme_dir is given in the settings.
#  5th: when the form had errors (remove the 'comment' field and add it empty.)


@pytest.mark.django_db
def test_post_js_comment_form_in_preview__no_reply_to__without_theme_dir(
    monkeypatch, rf, an_article, an_user
):
    monkeypatch.setattr(
        views, "json_res", lambda req, tmpl_list, ctx, **kwargs: tmpl_list
    )
    request = prepare_js_request_to_post_form(
        rf,
        an_article,
        an_user,
        remove_fields=["reply_to"],
        add_fields=[
            {"name": "preview", "value": 1},
            {"name": "reply_to", "value": 0},  # Will use 'form_js.html'.
        ],
    )
    template_list = views.post(request)
    assert template_list == [
        "comments/tests/article/form_js.html",
        "comments/tests/form_js.html",
        "comments/form_js.html",
    ]


@pytest.mark.django_db
def test_post_js_comment_form_in_preview__with_reply_to__without_theme_dir(
    monkeypatch, rf, an_article, an_user
):
    monkeypatch.setattr(
        views, "json_res", lambda req, tmpl_list, ctx, **kwargs: tmpl_list
    )
    request = prepare_js_request_to_post_form(
        rf,
        an_article,
        an_user,
        remove_fields=["reply_to"],
        add_fields=[
            {"name": "preview", "value": 1},
            {"name": "reply_to", "value": 1},  # Will use 'reply_form_js.html'.
        ],
    )
    template_list = views.post(request)
    assert template_list == [
        "comments/tests/article/reply_form_js.html",
        "comments/tests/reply_form_js.html",
        "comments/reply_form_js.html",
    ]


@pytest.mark.django_db
def test_post_js_comment_form_in_preview__no_reply_to__with_theme_dir(
    monkeypatch, rf, an_article, an_user
):
    monkeypatch.setattr(
        views.settings, "COMMENTS_INK_THEME_DIR", "feedback_in_header"
    )
    importlib.reload(views)

    monkeypatch.setattr(
        views, "json_res", lambda req, tmpl_list, ctx, **kwargs: tmpl_list
    )
    request = prepare_js_request_to_post_form(
        rf,
        an_article,
        an_user,
        remove_fields=["reply_to"],
        add_fields=[
            {"name": "preview", "value": 1},
            {"name": "reply_to", "value": 0},  # Will use 'form_js.html'.
        ],
    )
    template_list = views.post(request)
    assert template_list == [
        "comments/themes/feedback_in_header/tests/article/form_js.html",
        "comments/themes/feedback_in_header/tests/form_js.html",
        "comments/themes/feedback_in_header/form_js.html",
        "comments/tests/article/form_js.html",
        "comments/tests/form_js.html",
        "comments/form_js.html",
    ]

    # Revert theme.
    monkeypatch.setattr(views.settings, "COMMENTS_INK_THEME_DIR", "")
    importlib.reload(views)


@pytest.mark.django_db
def test_post_js_comment_form_in_preview__with_reply_to__with_theme_dir(
    monkeypatch, rf, an_article, an_user
):
    monkeypatch.setattr(
        views.settings, "COMMENTS_INK_THEME_DIR", "feedback_in_header"
    )
    importlib.reload(views)

    monkeypatch.setattr(
        views, "json_res", lambda req, tmpl_list, ctx, **kwargs: tmpl_list
    )
    request = prepare_js_request_to_post_form(
        rf,
        an_article,
        an_user,
        remove_fields=["reply_to"],
        add_fields=[
            {"name": "preview", "value": 1},
            {"name": "reply_to", "value": 1},  # Will use 'reply_form_js.html'.
        ],
    )
    template_list = views.post(request)
    assert template_list == [
        "comments/themes/feedback_in_header/tests/article/reply_form_js.html",
        "comments/themes/feedback_in_header/tests/reply_form_js.html",
        "comments/themes/feedback_in_header/reply_form_js.html",
        "comments/tests/article/reply_form_js.html",
        "comments/tests/reply_form_js.html",
        "comments/reply_form_js.html",
    ]

    # Revert theme.
    monkeypatch.setattr(views.settings, "COMMENTS_INK_THEME_DIR", "")
    importlib.reload(views)


@pytest.mark.django_db
def test_post_js_comment_form_with_an_empty_comment_field(
    rf, an_article, an_user
):
    request = prepare_js_request_to_post_form(
        rf,
        an_article,
        an_user,
        remove_fields=["comment"],
        add_fields=[
            {"name": "comment", "value": ""},
        ],
    )
    response = views.post(request)
    assert response.status_code == 200
    result = json.loads(response.content)
    assert result["field_focus"] == "comment"


# ---------------------------------------------------------------------
# Test that during the post_js of the comment form the signal
# 'comment_will_be_posted' has been called.
#  1. Check that when the signal returns False, a HTTP 400 is returned.
#  2. Check that wh nthe signal returns True, the comment is posted.


@pytest.mark.django_db
def test_post_js_comment_form__comment_will_be_posted__returns_400(
    monkeypatch, rf, an_article, an_user
):
    def mock_send(*args, **kwargs):
        class Receiver:
            def __init__(self):
                self.__name__ = "mocked receiver"

        return [
            (Receiver(), False),
        ]

    monkeypatch.setattr(views.comment_will_be_posted, "send", mock_send)
    request = prepare_js_request_to_post_form(rf, an_article, an_user)
    response = views.post(request)
    assert response.status_code == 400
    result = json.loads(response.content)
    assert result["html"].find("comment_will_be_posted receiver") > -1


@pytest.mark.django_db
def test_post_js_comment_form__comment_will_be_posted__returns_201(
    monkeypatch, rf, an_article, an_user
):
    def mock_send(*args, **kwargs):
        return [
            (None, True),
        ]

    monkeypatch.setattr(views.comment_will_be_posted, "send", mock_send)
    request = prepare_js_request_to_post_form(rf, an_article, an_user)
    response = views.post(request)
    assert response.status_code == 201
    result = json.loads(response.content)
    assert result["html"].find("Your comment has been already published.") > -1
    comment = InkComment.objects.get(pk=1)
    assert comment.user == an_user
