import React, {useEffect, useState} from 'react';
import {simulationApi} from './simulation-api.js';
import {actions} from './activity-data.js';

export function PolicyList({rules}) {
  return <div className="policy-catalog">{rules.map(rule => <details key={rule._id}>
    <summary><strong>{rule._id} · {rule.name.toLowerCase().replaceAll('_', ' ')}</strong>
      <span>{!rule.active && rule.active !== undefined ? 'Inactive' : rule.effect === 'deny' ? 'Block if failed' : 'Needs review if failed'}</span>
    </summary>
    <p>{rule.description}</p>
    <p>Applies to {rule.applies_to.includes('*') ? 'all actions' : rule.applies_to.map(action => actions[action] || action).join(', ')}.</p>
    {Object.keys(rule.params).length > 0 && <dl>{Object.entries(rule.params).map(([key, value]) => <div key={key}>
      <dt>{key.replaceAll('_', ' ')}</dt><dd>{Array.isArray(value) ? value.join(', ') : String(value)}</dd>
    </div>)}</dl>}
  </details>)}</div>;
}

export default function PolicyReview() {
  const [catalog, setCatalog] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    simulationApi.policies(controller.signal).then(setCatalog).catch(err => {
      if (!controller.signal.aborted) setError(err.message);
    });
    return () => controller.abort();
  }, []);

  return <section className="policy-review" aria-label="Policy review">
    <h2>Policy review</h2>
    <p>The Policy Gate automatically authorizes eligible actions. It blocks prohibited actions and sends high-impact actions to a supervisor.</p>
    {error && <p role="alert">{error}</p>}
    {!catalog && !error && <p role="status">Loading policies…</p>}
    {catalog && <><p>{catalog.bundled ? 'Built-in reference policies' : 'Active policy version'} {catalog.version} · {catalog.rules.length} rules</p>
      {catalog.bundled && <p>The connected backend does not report active policies. This catalog shows the repository defaults.</p>}
      <PolicyList rules={catalog.rules}/></>}
  </section>;
}
