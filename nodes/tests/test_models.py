import sure
from mock import MagicMock
from django.test import TestCase
from projects.models import User
from ..exceptions import TaskAlreadyPerformed
from .. import models
from . import factories


class NodeTaskCase(TestCase):
    """Node task case"""

    def setUp(self):
        self._mock_connect_to_node()
        self._mock_get_covio()
        self._mock_user_github()

    def _mock_connect_to_node(self):
        """Mock connect to node"""
        self._orig_connect_to_node = models.connect_to_node
        models.connect_to_node = MagicMock()
        self.node = MagicMock()
        self.node.execute.return_value = MagicMock(
            script='in', stdout='out', stderr='err',
        )
        models.connect_to_node.return_value.__enter__.return_value = self.node

    def _mock_get_covio(self):
        """Mock get_covio"""
        self._orig_get_covio = models.Project.get_covio
        models.Project.get_covio = MagicMock(return_value={'image': 'raw'})

    def _mock_user_github(self):
        """Mock user github"""
        self._orig_github_token = User.github_token
        User.github_token = 'token'

    def tearDown(self):
        models.connect_to_node = self._orig_connect_to_node
        models.Project.get_covio = self._orig_get_covio
        User.github_token = property(self._orig_github_token)

    def test_perform_task(self):
        """Test perform task"""
        task = factories.NodeTaskFactory()
        task.perform()
        task.state.should.be.equal(models.NodeTask.STATE_FINISHED)

    def test_set_input_and_outputs(self):
        """Test set inputs and outputs"""
        task = factories.NodeTaskFactory()
        task.perform()
        task.input.should.be.equal('in')
        task.stdout.should.be.equal('out')
        task.stderr.should.be.equal('err')

    def test_perform_only_new(self):
        """Test perform only new tasks"""
        task = factories.NodeTaskFactory(state=models.NodeTask.STATE_ACTIVE)
        task.perform.when.called_with().should.throw(TaskAlreadyPerformed)

    def test_set_failed_state_when_failed(self):
        """Test set failed state when failed"""
        self.node.execute.side_effect = Exception()
        task = factories.NodeTaskFactory()
        task.perform()
        task.state.should.be.equal(models.NodeTask.STATE_FAILED)
