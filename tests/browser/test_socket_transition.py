"""Late socket callbacks cannot touch a cleared or replacement workspace socket."""
import pytest

pytestmark = pytest.mark.browser


def test_late_socket_error_after_logout_and_reconnect(frontend):
    app = frontend(mode='editor')
    app.open()
    app.ready()
    result = app.page.evaluate('''() => {
      const previous = ws;
      const lateError = previous.onerror;
      let previousCloses = 0;
      const closePrevious = previous.close.bind(previous);
      previous.close = () => { previousCloses++; closePrevious(); };
      clearWorkspaceView();
      lateError();
      const cleared = ws === null;
      authReady = true;
      connectWebSocket();
      const replacement = ws;
      let replacementCloses = 0;
      const closeReplacement = replacement.close.bind(replacement);
      replacement.close = () => { replacementCloses++; closeReplacement(); };
      lateError();
      return {cleared, previousCloses, replacementCloses, replacementRetained:ws===replacement};
    }''')
    assert result == {'cleared': True, 'previousCloses': 3,
                      'replacementCloses': 0, 'replacementRetained': True}
