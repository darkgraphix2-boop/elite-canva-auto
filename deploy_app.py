import json
import os
from pathlib import Path

import uvicorn

data = Path(os.environ.get('ELITE_DATA_DIR', '/data'))
data.mkdir(parents=True, exist_ok=True)
os.environ['ELITE_DATA_DIR'] = str(data)
config_path = data / 'config.json'
if not config_path.exists():
    config = json.loads(os.environ['ELITE_CONFIG_JSON'])
    config_path.write_text(json.dumps(config), encoding='utf-8')
    config_path.chmod(0o600)

if __name__ == '__main__':
    uvicorn.run('main:app', host='0.0.0.0', port=int(os.environ.get('PORT', '8000')))
