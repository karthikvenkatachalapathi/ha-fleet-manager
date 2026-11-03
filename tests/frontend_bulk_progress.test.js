const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../fleet_manager/static/index.html'), 'utf8');

test('bulk acknowledgement is presented as accepted and not verified', () => {
  assert.match(source, /Bulk \$\{action\} accepted; not yet verified/);
  assert.match(source, /Accepted \$\{j\.queued\|\|0\}; .*not yet verified/);
  assert.match(source, /if\(j\.updated\)return `Verified/);
  assert.doesNotMatch(source, /setTimeout\(async\(\)=>\{await load\(\);route\('Updates'\)\}/);
});

test('durable bulk job id is polled from GET jobs and refreshed results replace the session snapshot', () => {
  assert.match(source, /sessionStorage\.setItem\('hafm-last-bulk-result',JSON\.stringify\(j\)\)/);
  assert.match(source, /api\('\/api\/jobs'\)/);
  assert.match(source, /String\(x\.id\|\|x\.job_id\)===String\(current\.job_id\)/);
  assert.match(source, /summary=job\.summary\|\|job\.details\|\|job/);
  assert.match(source, /setTimeout\(pollBulkJob,3000\)/);
  assert.match(source, /Job #\$\{esc\(j\.job_id\)\}/);
  assert.match(source, /Progress details will appear as the job is refreshed/);
});

test('bulk queued work remains visible in the operation dock before operation rows exist', () => {
  assert.match(source, /bulkActive=bulkIsActive\(bulk\)/);
  assert.match(source, /if\(!ops\.length&&!bulkActive\)/);
  assert.match(source, /Bulk \$\{esc\(bulk\.action\|\|'update'\)\} · \$\{esc\(waiting\?'waiting for restart':bulk\.status\)\}/);
  assert.match(source, /let count=ops.length/);
  assert.match(source, /Batch waiting for restart/);
  assert.match(source, /\['queued','running'\]\.includes\(String\(j\.status\|\|''\)\.toLowerCase\(\)\)/);
});

test('summary separates verified completion, action-required outcomes, and accepted work', () => {
  assert.match(source, /Action required for \$\{j\.action_required\} update/);
  assert.match(source, /Verified \$\{j\.updated\} update/);
  assert.match(source, /Accepted \$\{j\.queued\|\|0\}/);
  for (const count of ['selected','updated','skipped','reviewed','action_required']) {
    assert.ok(source.includes(`'${count}'`), `missing ${count} count`);
  }
});
