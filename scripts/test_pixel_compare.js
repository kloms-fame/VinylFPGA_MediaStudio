const assert=require('node:assert/strict');
let compare;try{compare=require('../web/pixel-compare.js').compareRgba;}catch{}
assert.equal(typeof compare,'function','Exact RGB pixel comparison is not implemented');
const a=new Uint8ClampedArray(640*480*4);for(let i=3;i<a.length;i+=4)a[i]=255;
const b=a.slice();
assert.equal(compare(a,b).differentPixels,0);
b[(100*640+200)*4]=1;
let r=compare(a,b);assert.equal(r.differentPixels,1);assert.equal(r.maximumChannelError,1);
assert.deepEqual(r.firstDifference,{x:200,y:100,left:[0,0,0],right:[1,0,0]});
b[(100*640+200)*4+1]=255;r=compare(a,b);assert.equal(r.differentPixels,1);assert.equal(r.maximumChannelError,255);
assert.throws(()=>compare(a.slice(4),b),/640/);
console.log('PASS exact pixel comparison: identity, one-pixel difference, channel bound and dimensions');
