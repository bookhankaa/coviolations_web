from datetime import datetime, timedelta
from pymongo import DESCENDING
from html2text import html2text
from github import Github
from django.core.urlresolvers import reverse
from django.template.loader import render_to_string
from django.contrib.sites.models import Site
from django.conf import settings
from django_rq import job
from violations.exceptions import ViolationDoesNotExists
import services.base
import violations.base
from push.base import sender
from projects.models import Project
from .models import Tasks
from .utils import logger
from . import const


def _prepare_single_commit(commit):
    """Prepare single commit"""
    return {
        'author': {
            'avatar': commit.author.avatar_url,
            'name': commit.author.name,
            'url': commit.author.html_url,
        },
        'message': commit.commit.message,
        'hash': commit.sha[:6],
        'url': commit.html_url,
    }


def _fill_task_from_github(task_id):
    """Fill task from github"""
    task = Tasks.find_one(task_id)
    github = Github(
        client_id=settings.GITHUB_APP_ID,
        client_secret=settings.GITHUB_API_SECRET,
    )
    repo = github.get_repo(task['project'])
    collection = Tasks.find({
        'project': task['project'],
        'commit.branch': task['commit']['branch'],
        'commit.hash': {'$ne': task['commit']['hash']},
        'created': {'$lte': task['created'] + timedelta(seconds=5)},
    }, sort=[('created', DESCENDING)])
    if collection.count():
        previous = collection[0]['commit']['hash']
        comparison = repo.compare(
            previous, task['commit']['hash'],
        )
        task['commit']['url'] = comparison.html_url
        task['commit']['range'] = '{}..{}'.format(
            previous[:6],
            task['commit']['hash'][:6],
        )
        task['commit']['inner'] = map(
            _prepare_single_commit, comparison.commits,
        )
    else:
        commit = repo.get_commit(task['commit']['hash'])
        task['commit']['url'] = commit.html_url
        task['commit']['range'] = task['commit']['hash'][:6]
        task['commit']['inner'] = [_prepare_single_commit(commit)]
    Tasks.save(task)


@job
def create_task(task_id):
    """Create task job"""
    data = Tasks.find_one(task_id)
    data['created'] = datetime.now()
    data['status'] = const.STATUS_NEW
    task = services.base.library.get(data['service']['name'])(data)

    if task:
        _fill_task_from_github(task_id)
        prepare_violations.delay(task)
    else:
        logger.warning(
            'Task failed: {}'.format(task_id), exc_info=True, extra={
                'task': task
            },
        )


def _prepare_violation(violation):
    """Prepare single violation"""
    try:
        violation_creator = violations.base.library.get(violation['name'])
        return violation_creator(violation)
    except ViolationDoesNotExists as e:
        logger.warning(
            "Violation doesn't exists: {}".format(e), exc_info=True, extra={
                'violation': violation,
            },
        )

        violation['status'] = const.STATUS_FAILED
        return violation
    except Exception as e:
        logger.exception('Violation failed: {}'.format(e))
        return violation


@job
def prepare_violations(task_id):
    """Prepare violations"""
    task = Tasks.find_one(task_id)
    task['violations'] = map(_prepare_violation, task['violations'])
    task['status'] = const.STATUS_SUCCESS if all([
        violation.get('status') != const.STATUS_FAILED
        for violation in task['violations']
    ]) else const.STATUS_FAILED
    Tasks.save(task)

    sender.send(
        type='task', owner=task['owner_id'],
        task=str(task_id), project=task['project'],
    )

    if task.get('pull_request_id') and not task.get('is_private'):
        comment_pull_request.delay(task_id)


@job
def comment_pull_request(task_id):
    """Comment pull request on github"""
    task = Tasks.find_one(task_id)
    project = Project.objects.get(name=task['project'])
    github = Github(
        settings.GITHUB_COMMENTER_USER, settings.GITHUB_COMMENTER_PASSWORD,
    )
    repo = github.get_repo(project.name)
    pull_request = repo.get_pull(task['pull_request_id'])
    html_comment = render_to_string(
        'tasks/pull_request_comment.html', {
            'badge': project.get_badge_url(commit=task['commit']['hash']),
            'task': task,
            'url': 'http://{}{}'.format(
                Site.objects.get_current().domain,
                reverse('tasks_detail', args=(str(task['_id']),)),
            ),
        }
    )
    pull_request.create_issue_comment(
        html2text(html_comment)
    )
