/* Compare original RGB values without resizing, smoothing or tolerance. */
(function(root){
  function compareRgba(left,right){
    const width=640,height=480,length=width*height*4;
    if(left.length!==length||right.length!==length)throw new Error('对照必须是两张原生 640×480 图像');
    const difference=new Uint8ClampedArray(length);
    let differentPixels=0,maximumChannelError=0,firstDifference=null;
    for(let i=0;i<length;i+=4){
      if(left[i+3]!==255||right[i+3]!==255)throw new Error('请使用不透明的原始 HDMI PNG 截图');
      let changed=false;
      for(let c=0;c<3;c++){const delta=Math.abs(left[i+c]-right[i+c]);difference[i+c]=Math.min(255,delta*8);maximumChannelError=Math.max(maximumChannelError,delta);changed ||= delta!==0;}
      difference[i+3]=255;
      if(changed){differentPixels++;if(!firstDifference)firstDifference={x:(i/4)%width,y:Math.floor(i/4/width),left:Array.from(left.slice(i,i+3)),right:Array.from(right.slice(i,i+3))};}
    }
    return {width,height,totalPixels:width*height,differentPixels,maximumChannelError,firstDifference,difference};
  }
  root.PixelCompare={compareRgba};
  if(typeof module!=='undefined')module.exports={compareRgba};
})(typeof window!=='undefined'?window:globalThis);
