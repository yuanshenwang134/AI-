const assert = require('node:assert/strict');
global.window = {STORE:{}};
require(process.argv[2] || require('node:path').resolve(__dirname, '../web/pages/review.js'));
const page=window.PAGES.review;
const vm={...page.data(), $message:{info(){},error(){},success(){},warning(){}}};
for(const [k,v] of Object.entries(page.methods)) vm[k]=v.bind(vm);
const suggestion={result:'unknown',made:false,suggested_made:true,evidence:'cross_extrapolated'};
assert.equal(vm.resultLabel(suggestion),'结果未知');
assert.equal(vm.hasSuggestion(suggestion),true);
assert.equal(vm.hasSuggestion({...suggestion,result:'made',made:true}),false);
assert.equal(vm.hasSuggestion({result:'unknown',made:false}),false);
vm.rows=[suggestion,{result:'missed',made:false,value:2}];
vm.onlyInferred=true;
assert.equal(page.computed.list.call(vm).length,1);
(async()=>{
 let calls=[]; vm.correct=async(r,m)=>calls.push(m);
 await vm.confirmAll(); assert.deepEqual(calls,[false]);
 const errors=[];
 vm.$message.error=m=>errors.push(m);
 vm.canSubmit=true;vm.S.jobId='fixture';vm.busyIdx=-1;
 window.API={correctShot:async()=>{throw Error('test failure')}};
 vm.applyLocal=()=>{throw Error('must not apply failed request')};
 await page.methods.correct.call(vm,{_index:0},true,2);
 assert.equal(vm.busyIdx,-1);assert.equal(errors.length,1);
 console.log('Review suggestions: 7 behavior checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});

for (const [code, text] of [['cross_rim_contact','筐沿'],['cross_hoop_uncertain','篮筐位置']]) {
  for (const fields of [{evidence:code},{tags:[code]}]) {
    const row={result:'unknown',made:null,suggested_made:true,...fields};
    assert.ok(vm.unknownReason(row).includes(text));
    assert.equal(vm.hasSuggestion(row),false,'未知原因不能误呈现成可采纳建议');
    assert.equal(vm.unknownReason({...row,result:'made',made:true}),'');
  }
}
assert.ok(page.template.includes('v-if="unknownReason(row)"'));
assert.ok(page.template.includes('判定依据：'));
console.log('Unknown evidence reasons and suggestion exclusion passed');
const reasonBlock = page.template.split('v-if="unknownReason(row)"')[1].split('v-if="hasSuggestion(row)"')[0];
assert.ok(reasonBlock.includes('v-if="row.crossing_t != null"'));
assert.ok(reasonBlock.includes('建议核对 {{ mmss(row.crossing_t) }} 附近画面'));

const gapRow={result:'unknown',made:null,suggested_made:true,evidence:'legacy_occlusion_descent',review_t:16.483,crossing_t:null};
assert.equal(vm.hasSuggestion(gapRow),true);
assert.equal(vm.suggestionReviewTime(gapRow),16.483);
assert.ok(vm.suggestionReason(gapRow).includes('尚未确认穿筐'));
assert.equal(vm.suggestionReviewTime({crossing_t:3}),3);
assert.equal(vm.suggestionReviewTime({review_t:0,crossing_t:3}),0);
assert.equal(vm.suggestionReviewTime({}),null);
assert.equal(vm.suggestionReviewTime({review_t:NaN}),null);
assert.equal(vm.suggestionReviewTime({review_t:-1}),null);
assert.ok(page.template.includes('mmss(suggestionReviewTime(row))'));
console.log('Gap suggestion review time and copy passed');
