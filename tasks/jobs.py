from datetime import datetime, timedelta
from pymongo import DESCENDING
from html2text import html2text
from github import Github
from django.template.loader import render_to_string
from django.conf import settings
from django_rq import job
from tools.short import make_https
from violations.exceptions import ViolationDoesNotExists
import services.base
import violations.base
from push.base import sender
from projects.models import Project
from .models import Tasks
from .utils import logger
from . import const


def _pretty_author(author):
    """Get pretty author"""
    if author.name and author.login:
        template = '{author.name} ({author.login})'
    elif author.name:
        template = '{author.name}'
    else:
        template = '{author.login}'
    return template.format(author=author)


def _prepare_single_commit(commit):
    """Prepare single commit"""
    return {
        'author': {
            'avatar': commit.author.avatar_url,
            'name': _pretty_author(commit.author),
            'url': commit.author.html_url,
        },
        'message': commit.commit.message,
        'hash': commit.sha[:6],
        'url': commit.html_url,
    }


def _fill_task_from_github(task_id):
    """Fill task from github"""
    task = Tasks.find_one(task_id)
    project = Project.objects.get(name=task['project'])
    collection = Tasks.find({
        'project': task['project'],
        'commit.branch': task['commit']['branch'],
        'commit.hash': {'$ne': task['commit']['hash']},
        'created': {'$lte': task['created'] + timedelta(seconds=5)},
    }, sort=[('created', DESCENDING)])
    if collection.count():
        previous = collection[0]['commit']['hash']
        comparison = project.repo.compare(
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
        commit = project.repo.get_commit(task['commit']['hash'])
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
        result = violation_creator(violation)
    except ViolationDoesNotExists as e:
        logger.warning(
            "Violation doesn't exists: {}".format(e), exc_info=True, extra={
                'violation': violation,
            },
        )

        violation['status'] = const.STATUS_FAILED
        result = violation
    except Exception as e:
        logger.exception('Violation failed: {}'.format(e))
        result = violation

    if result.get('nofail', False):
        violation['status'] = const.STATUS_SUCCESS
    if result.get('success_percent') is None:
        result['success_percent'] =\
            100 if result['status'] == const.STATUS_SUCCESS else 0
    return result


@job
def prepare_violations(task_id):
    """Prepare violations"""
    task = Tasks.find_one(task_id)
    task['violations'] = map(_prepare_violation, task['violations'])
    task['status'] = const.STATUS_SUCCESS if all([
        violation.get('status') != const.STATUS_FAILED
        for violation in task['violations']
    ]) else const.STATUS_FAILED
    success_percents = [
        violation['success_percent'] for violation in task['violations']
    ]
    task['success_percent'] = sum(success_percents) / len(success_percents)\
        if success_percents else 100
    Tasks.save(task)

    mark_commit_with_status.delay(task_id)
    if task.get('pull_request_id'):
        comment_pull_request.delay(task_id)
    comment_lines.delay(task_id)

    project = Project.objects.get(name=task['project'])
    project.update_week_statistic()
    project.update_day_time_statistic()
    project.update_quality_game(task)

    sender.send(
        type='task', owner=task['owner_id'],
        task=str(task_id), project=task['project'],
    )


@job
def comment_pull_request(task_id):
    """Comment pull request on github"""
    task = Tasks.find_one(task_id)
    project = Project.objects.get(name=task['project'])
    if task.get('is_private'):
        if project.comment_from_owner_account:
            github = project.owner.github
        else:
            return
    else:
        github = Github(
            settings.GITHUB_COMMENTER_USER, settings.GITHUB_COMMENTER_PASSWORD,
        )
    repo = github.get_repo(project.name)
    pull_request = repo.get_pull(task['pull_request_id'])
    html_comment = render_to_string(
        'tasks/pull_request_comment.html', {
            'badge': project.get_badge_url(commit=task['commit']['hash']),
            'task': task,
            'url': make_https('/#/tasks/{}/'.format(task['_id'])),
        }
    )
    pull_request.create_issue_comment(
        html2text(html_comment)
    )


@job
def mark_commit_with_status(task_id):
    """Mark commit with status"""
    task = Tasks.find_one(task_id)
    project = Project.objects.get(name=task['project'])
    commit = project.repo.get_commit(task['commit']['hash'])
    commit.create_status(
        const.GITHUB_STATES.get(task['status'], 'error'),
        make_https('/#/tasks/{}/'.format(task['_id'])),
        (
            const.GITHUB_DESCRIPTION_OK
            if task['status'] == const.STATUS_SUCCESS else
            const.GITHUB_DESCRIPTION_FAIL
        ),
    )


@job
def comment_lines(task_id):
    """Comment lines of code"""
    task = Tasks.find_one(task_id)
    lines = reduce(
        lambda acc, violation: acc + violation.get('lines', []),
        task['violations'], [],
    )
    if not len(lines):
        return

    project = Project.objects.get(name=task['project'])
    if task.get('is_private'):
        if project.comment_from_owner_account:
            github = project.owner.github
        else:
            return
    else:
        github = Github(
            settings.GITHUB_COMMENTER_USER, settings.GITHUB_COMMENTER_PASSWORD,
        )
    repo = github.get_repo(project.name)
    commit = repo.get_commit(task['commit']['hash'])
    exists_comments = list(commit.get_comments())
    for line in lines:
        if len(filter(
            lambda comment: comment.body == line['body']
            and comment.line == line['line'] and comment.path == line['path']
            and comment.position == line['position'], exists_comments,
        )):
            continue

        commit.create_comment(
            line['body'], line['line'], line['path'], line.get('position', 0),
        )
