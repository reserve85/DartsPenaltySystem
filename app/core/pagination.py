"""Shared list pagination size.

Rule: the long management lists (assignment matrix, matchdays) show UP TO 100
rows per page so a whole season fits on one screen; small technical lists
(users, audit log) keep their own smaller ``paginate_by``.
"""

PAGE_SIZE = 100
