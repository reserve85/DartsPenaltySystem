"""UserLanguageMiddleware — language selection beyond LocaleMiddleware.

Precedence (spec H2 — "explicit session/cookie choice takes precedence"):

1. **Explicit language cookie** — wins everywhere.
2. **Language prefix in the URL** (``/en/...``) — wins over the stored profile
   preference (a specific page URL is always rendered in its own language).
3. **``user.preferred_language``** — applied on unprefixed URLs without a
   cookie.

Because ``i18n_patterns(..., prefix_default_language=False)`` registers
unprefixed URLs *only* for the default language, a desired language other than
the default can never render in place on such a path — the URL would not even
resolve. Therefore GET/HEAD requests are **redirected to the translated URL**
(``translate_url``), which keeps every bookmarked/foreign URL working and all
subsequent links consistently prefixed (``/en/...``). Non-GET requests are
left untouched so form submissions are never converted to GETs by a redirect;
the preference is applied again on the following GET.

Runs after ``AuthenticationMiddleware`` (``request.user``) and after
``LocaleMiddleware`` (cookie/path already inspected). Django 5.2 reads the
language override solely from ``LANGUAGE_COOKIE_NAME``.
"""

from django.conf import settings
from django.http import HttpResponseRedirect
from django.urls import translate_url
from django.utils import translation


def _is_valid_language(code) -> bool:
    if not code:
        return False
    return any(code == lang_code for lang_code, _name in settings.LANGUAGES)


class UserLanguageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        desired = self._desired_language(request)
        if desired and desired != translation.get_language() and request.method in ("GET", "HEAD"):
            translated = translate_url(request.get_full_path(), desired)
            if translated != request.get_full_path():
                return HttpResponseRedirect(translated)
            translation.activate(desired)
            request.LANGUAGE_CODE = desired
        # Non-GET requests keep the URL language so redirects never mangle
        # form submissions; the next GET applies the desired language.
        return self.get_response(request)

    @staticmethod
    def _desired_language(request) -> str | None:
        cookie = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
        if _is_valid_language(cookie):
            return cookie
        if translation.get_language_from_path(request.path_info):
            return None  # explicit /xx/ URL prefix wins over the profile preference
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            preferred = getattr(user, "preferred_language", None)
            if _is_valid_language(preferred):
                return preferred
        return None
