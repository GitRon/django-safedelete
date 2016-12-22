from django.contrib import admin
from django.contrib.admin.sites import AdminSite
from django.contrib.admin.views.main import ChangeList
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.test import RequestFactory, TestCase

from ..admin import SafeDeleteAdmin, highlight_deleted
from ..config import (DELETED_VISIBLE_BY_PK, HARD_DELETE,
                      HARD_DELETE_NOCASCADE, NO_DELETE, SOFT_DELETE)
from ..managers import SafeDeleteManager, SafeDeleteQueryset
from ..models import SafeDeleteMixin


# MODELS (FOR TESTING)


class Author(SafeDeleteMixin):
    _safedelete_policy = HARD_DELETE_NOCASCADE

    name = models.CharField(max_length=200)


class CategoryManager(SafeDeleteManager):
    _safedelete_visibility = DELETED_VISIBLE_BY_PK


class Category(SafeDeleteMixin):
    _safedelete_policy = SOFT_DELETE

    name = models.CharField(max_length=200, unique=True)

    objects = CategoryManager()

    def __str__(self):
        return self.name


class Article(SafeDeleteMixin):
    _safedelete_policy = HARD_DELETE

    name = models.CharField(max_length=200)
    author = models.ForeignKey(Author)
    category = models.ForeignKey(Category, null=True, default=None)

    def __unicode__(self):
        return 'Article ({0}): {1}'.format(self.pk, self.name)


class Order(SafeDeleteMixin):
    name = models.CharField(max_length=100)
    articles = models.ManyToManyField(Article)


class VeryImportant(SafeDeleteMixin):
    _safedelete_policy = NO_DELETE

    name = models.CharField(max_length=200)


class CustomQueryset(SafeDeleteQueryset):

    def best(self):
        return self.filter(color='green')


class CustomManager(SafeDeleteManager):

    def get_queryset(self):
        queryset = CustomQueryset(self.model, using=self._db)
        return queryset.filter(deleted__isnull=True)

    def best(self):
        return self.get_queryset().best()


class HasCustomQueryset(SafeDeleteMixin):
    name = models.CharField(max_length=200)
    color = models.CharField(max_length=5, choices=(('red', 'Red'), ('green', 'Green')))

    objects = CustomManager()


# ADMINMODEL (FOR TESTING)


class CategoryAdmin(SafeDeleteAdmin):
    list_display = (highlight_deleted,) + SafeDeleteAdmin.list_display


admin.site.register(Category, CategoryAdmin)


# TESTS


class SimpleTest(TestCase):

    def setUp(self):

        self.authors = (
            Author.objects.create(name='author 0'),
            Author.objects.create(name='author 1'),
            Author.objects.create(name='author 2'),
        )

        self.categories = (
            Category.objects.create(name='category 0'),
            Category.objects.create(name='category 1'),
            Category.objects.create(name='category 2'),
        )

        self.articles = (
            Article.objects.create(name='article 0', author=self.authors[1]),
            Article.objects.create(name='article 1', author=self.authors[
                                   1], category=self.categories[1]),
            Article.objects.create(name='article 2', author=self.authors[
                                   2], category=self.categories[2]),
        )

        self.order = Order.objects.create(name='order')
        self.order.articles.add(self.articles[0], self.articles[1])

    def test_prefetch_related(self):
        """ prefetch_related() queryset should not be filtered by core_filter """
        authors = Author.objects.all().prefetch_related('article_set')
        for author in authors:
            self.assertQuerysetEqual(
                author.article_set.all().order_by('pk'),
                [repr(a) for a in Author.objects.get(pk=author.pk).article_set.all().order_by('pk')]
            )

    def test_validate_unique(self):
        """ Check that uniqueness is also checked against deleted objects """
        Category.objects.create(name='test').delete()
        with self.assertRaises(ValidationError):
            Category(name='test').validate_unique()


class AdminTestCase(TestCase):
    urls = 'safedelete.tests.urls'

    def setUp(self):
        self.author = Author.objects.create(name='author 0')
        self.categories = (
            Category.objects.create(name='category 0'),
            Category.objects.create(name='category 1'),
            Category.objects.create(name='category 2'),
        )
        self.articles = (
            Article(name='article 0', author=self.author),
            Article(name='article 1', author=self.author, category=self.categories[1]),
            Article(name='article 2', author=self.author, category=self.categories[2]),
        )
        self.categories[1].delete()
        self.request_factory = RequestFactory()
        self.request = self.request_factory.get('/', {})
        self.modeladmin_default = admin.ModelAdmin(Category, AdminSite())
        self.modeladmin = CategoryAdmin(Category, AdminSite())
        User.objects.create_superuser("super", "", "secret")
        self.client.login(username="super", password="secret")

    def tearDown(self):
        self.client.logout()

    def get_changelist(self, request, model, modeladmin):
        return ChangeList(
            request, model, modeladmin.list_display,
            modeladmin.list_display_links, modeladmin.list_filter,
            modeladmin.date_hierarchy, modeladmin.search_fields,
            modeladmin.list_select_related, modeladmin.list_per_page,
            modeladmin.list_max_show_all, modeladmin.list_editable,
            modeladmin
        )

    def test_admin_model(self):
        changelist_default = self.get_changelist(self.request, Category, self.modeladmin_default)
        changelist = self.get_changelist(self.request, Category, self.modeladmin)
        self.assertEqual(changelist.get_filters(self.request)[0][0].title, "deleted")
        self.assertEqual(changelist.queryset.count(), 3)
        self.assertEqual(changelist_default.queryset.count(), 2)

    def test_admin_listing(self):
        """ Test deleted objects are in red in admin listing. """
        resp = self.client.get('/admin/safedelete/category/')
        line = '<span class="deleted">{0}</span>'.format(self.categories[1])
        self.assertContains(resp, line)

    def test_admin_xss(self):
        Category.objects.create(name='<script>alert(42)</script>'),
        resp = self.client.get('/admin/safedelete/category/')
        # It should be escaped
        self.assertNotContains(resp, '<script>alert(42)</script>')

    def test_admin_undelete_action(self):
        """ Test objects are undeleted and action is logged. """
        resp = self.client.post('/admin/safedelete/category/', data={
            'index': 0,
            'action': ['undelete_selected'],
            '_selected_action': [self.categories[1].pk],
        })
        self.assertTemplateUsed(resp, 'safedelete/undelete_selected_confirmation.html')
        category = Category.objects.get(pk=self.categories[1].pk)
        self.assertTrue(self.categories[1].deleted)

        resp = self.client.post('/admin/safedelete/category/', data={
            'index': 0,
            'action': ['undelete_selected'],
            'post': True,
            '_selected_action': [self.categories[1].pk],
        })
        category = Category.objects.get(pk=self.categories[1].pk)
        self.assertFalse(category.deleted)
