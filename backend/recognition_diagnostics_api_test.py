"""Offline owner isolation for structural diagnostics (no browser/model)."""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from app import main, storage


def run():
    with TemporaryDirectory() as directory, \
            patch.object(storage, 'DATA_DIR', Path(directory)), \
            patch.object(storage, 'DB_PATH', Path(directory) / 'fixture.db'), \
            patch.object(main, 'UPLOAD_DIR', Path(directory) / 'uploads'), \
            patch.dict(os.environ, {'APP_AUTH_REQUIRED':'false'}), \
            patch.object(main.browser_demo, 'close', new_callable=AsyncMock), \
            patch.object(main.browser_demo, 'recognition_diagnostics', new_callable=AsyncMock,
                         return_value={'controls':[]}) as inspect, \
            TestClient(main.app) as client:
        user = client.get('/api/auth/me').json()['id']
        with patch.dict(main.browser_session_owners, {'fixture':user}, clear=True):
            response=client.get('/api/browser/fixture/recognition-diagnostics')
            assert response.status_code==200 and response.json()=={'controls':[]}
            assert inspect.await_count==1
        with patch.dict(main.browser_session_owners, {'fixture':'another-user'}, clear=True):
            assert client.get('/api/browser/fixture/recognition-diagnostics').status_code==404
            assert inspect.await_count==1
    print('recognition_diagnostics_api_test: OK (owner only; no browser/model access)')


if __name__=='__main__':
    run()
