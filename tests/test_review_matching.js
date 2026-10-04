const assert = require('node:assert/strict');
global.window={STORE:{}};
require(process.argv[2] || require('node:path').resolve(__dirname,'../web/pages/review.js'));
const p=window.PAGES.review;
const errors=[];
const vm={...p.data(),$message:{error:m=>errors.push(m)}};
for (const [k,v] of Object.entries(p.methods)) vm[k]=v.bind(vm);
function match(timeline, row) {
 vm.game={timeline,needs_review:[row]}; vm.build(); return vm.rows[0]._index;
}
const event={t:1.23,player_id:'H1',value:2};
assert.equal(match([event],{...event,t:1.2345}),0);
assert.equal(match([event],{...event,value:3}),0);
assert.equal(match([event,event],{...event}),-1);
assert.equal(match([event,event],{...event,index:1}),1);
assert.equal(match([event],{...event,index:99}),0);
assert.equal(match([event],{...event,player_id:'H2'}),-1);
(async()=>{
 await vm.correct({_index:-1},true,2);
 assert.equal(errors.length,1); assert.match(errors[0],/刷新后重试/);
 console.log('Review event matching: 7 checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
