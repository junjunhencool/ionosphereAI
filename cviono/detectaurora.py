#!/usr/bin/env python
"""
Michael Hirsch Dec 2014
This program detects aurora in multi-terabyte raw video data files
It is also used for the Haystack passive FM radar ionospheric activity detection
"""
from __future__ import division, absolute_import
import logging
import cv2
print('OpenCV {}'.format(cv2.__version__)) #some installs of OpenCV don't give a consistent version number, just a build number and I didn't bother to parse this.
#
import h5py
from datetime import datetime
from pytz import UTC
from pandas import DataFrame
from . import Path
import numpy as np
from scipy.signal import wiener
from scipy.misc import bytescale
from time import time
from matplotlib.pylab import draw, pause, figure, hist,close
#
from .io import getparam,getvidinfo
from .cvops import dooptflow,dothres,dodespeck,domorph,doblob
from .cvsetup import setupkern,svsetup,svrelease,setupof,setupfigs,statplot
from .getpassivefm import getfmradarframe
#
from cvutils.connectedComponents import setupblob
#
from histutils.rawDMCreader import getDMCframe
#plot disable
pshow = ('thres',
         'stat',
         'final',
         'det','savedet')
#'raw' #often not useful due to no autoscale
#'rawscaled'      #True  #why not just showfinal
#'hist' ogram
# 'flowvec'
#'flowhsv'
#'thres'
#'morph'
#'final'
#'det'
#'savedet' #save detection results (normally use this)
complvl = 4 #tradeoff b/w speed and filesize for TIFF


def loopaurorafiles(flist, up,savevideo, framebyframe, verbose):
    if not flist:
        raise ValueError('no files found')

    P = getparam(up['paramfn'])

    aurstat = DataFrame(columns=['mean','median','variance','detect'])

    for f in flist: #iterate over files in list
        stat = procaurora(f, P, up, savevideo,framebyframe,verbose)
        aurstat = aurstat.append(stat)
#%% sort,plot,save results for all files
    aurstat.sort_index(inplace=True) #sort by time
    savestat(aurstat,up['detfn'])

    if stat.index[0] > 1e9: #ut1 instead of index
        dt = [datetime.fromtimestamp(t,tz=UTC) for t in stat.index]
    else:
        dt=None

    fgst = statplot(dt,aurstat.index,aurstat['mean'],aurstat['median'],aurstat['detect'],
             fn=up['outdir'],pshow='stat')[3]
    draw(); pause(0.001)
    fgst.savefig(up['detfn'].with_suffix('.png'),bbox_inches='tight',dpi=100)

    return aurstat


def procaurora(f, P,up,savevideo,framebyframe,verbose=False):
    tic = time()

    finf,ap,dfid = getvidinfo(f, P,up,verbose)
    if finf is None:
        return
#%% setup optional video/tiff writing (mainly for debugging or publication)
    svh = svsetup(savevideo,complvl, ap, P, up,pshow)
#%% setup blob
    blobdetect = setupblob(P['minblobarea'], P['maxblobarea'], P['minblobdist'])
#%% cv opt. flow matrix setup
    uv, lastflow,gmm = setupof(ap,P)
    isgmm = lastflow is None
#%% kernel setup
    kern = setupkern(ap, P)
#%% mag plots setup
    pl,stat = setupfigs(finf,f,pshow)
#%% start main loop
    for jfrm,ifrm in enumerate(finf['frameind'][:-1]):
#%% load and filter
        try:
            framegray,frameref,ap = getraw(dfid,ifrm,finf,svh,ap, P,savevideo,verbose)
        except Exception:
            break
#%% compute optical flow or Background/Foreground
        if ~isgmm:
            flow,ofmaggmm,stat = dooptflow(framegray,frameref,lastflow,uv,
                                               ifrm,jfrm, ap, P,pl,stat,pshow)
            lastflow = flow.copy() #I didn't check if the .copy() is strictly necessary
        else: #background/foreground
            ofmaggmm = gmm.apply(framegray)
#%% threshold
        thres = dothres(ofmaggmm, stat['median'].iat[jfrm],ap, P,svh,pshow,isgmm)
#%% despeckle
        despeck = dodespeck(thres,P['medfiltsize'],svh,pshow)
#%% morphological ops
        morphed = domorph(despeck,kern,svh,pshow)
#%% blob detection
        stat = doblob(morphed,blobdetect,framegray,ifrm,jfrm,svh,pl,stat,pshow) #lint:ok
#%% plotting in loop
        """
        http://docs.opencv.org/modules/highgui/doc/user_interface.html
        """
        draw(); pause(0.001)

        if not ifrm % 50:
            print('frame {:0d}'.format(ifrm))
            if (framegray == 255).sum() > 40: #arbitrarily allowing up to 40 pixels to be saturated at 255, to allow for bright stars and faint aurora
                print('* Warning: video may be saturated at value 255, missed detections can result')
            if (framegray == 0).sum() > 4:
                print('* Warning: video may be saturated at value 0, missed detections can result')

        if framebyframe: #wait indefinitely for spacebar press
            keypressed = cv2.waitKey(0)
            framebyframe,dobreak = keyhandler(keypressed,framebyframe)
        else:
            keypressed = cv2.waitKey(1)
            framebyframe, dobreak = keyhandler(keypressed,framebyframe)
        if dobreak:
            break
#%% done looping this file

#%% close data file
    try:
        if finf['reader'] in ('raw','fits'):
            dfid.close()
        elif finf['reader'] == 'cv2':
            dfid.release()
    except Exception as e:
        print(str(e))
#%% save results for this file
    print('{:.1f} seconds to process {}'.format(time()-tic,f))

    if 'savedet' in pshow:
        detfn =    Path(up['outdir']).expanduser()/(f.stem +'_detections.h5')
        detpltfn = Path(up['outdir']).expanduser()/(f.stem +'_detections.png')
        if detfn.is_file():
            logging.warning('overwriting existing %s', detfn)

        try:
            savestat(stat,detfn)

            print('saving detection plot to {}'.format(detpltfn))
            pl['fdet'].savefig(str(detpltfn),dpi=100,bbox_inches='tight')

        except Exception as e:
            logging.critical('trouble saving detection result   '.format(e))
        finally:
            svrelease(svh,savevideo)

    close('all')

    return stat


def keyhandler(keypressed,framebyframe):
    if keypressed == -1: # no key pressed
        return (framebyframe,False)
    elif keypressed == 1048608: #space
        return (not framebyframe, False)
    elif keypressed == 1048603: #escape
        return (None, True)
    else:
        print('keypress code: ' + str(keypressed))
        return (framebyframe,False)

def savestat(stat,fn):
    assert isinstance(stat,DataFrame)
    print('saving detections & statistics to {}'.format(fn))

    with h5py.File(str(fn),'w',libver='latest') as h5:
        h5['/detect']  = stat['detect']
        h5['/mean']    = stat['mean']
        h5['/median']  = stat['median']
        h5['/variance']= stat['variance']

def getraw(dfid,ifrm,finf,svh,ap,cp,savevideo,verbose):
    """ this function reads the reference frame too--which makes sense if youre
       only reading every Nth frame from the multi-TB file instead of every frame
    """
    frameref = None #just in case not used
    dowiener = np.isfinite(cp['wienernhood'])
#%% reference frame

    if finf['reader'] == 'raw' and dfid:
        if ap['twoframe']:
            frameref = getDMCframe(dfid,ifrm,finf,verbose)[0]
            frameref = bytescale(frameref, ap['rawlim'][0], ap['rawlim'][1])
            if dowiener:
                frameref = wiener(frameref,cp['wienernhood'])

        try:
            frame16,rfi = getDMCframe(dfid,ifrm+1,finf)
            framegray = bytescale(frame16, ap['rawlim'][0], ap['rawlim'][1])
        except (ValueError,IOError):
            ap['rawframeind'] = np.delete(ap['rawframeind'], np.s_[ifrm:])
            raise

    elif finf['reader'] == 'cv2':
        if ap['twoframe']:
            retval,frameref = dfid.read()
            if not retval:
                if ifrm==0:
                    logging.error('could not read video file, sorry')
                print('done reading video.')
                return None, None, ap
            if frameref.ndim>2:
                frameref = cv2.cvtColor(frameref, cv2.COLOR_RGB2GRAY)
            if dowiener:
                frameref = wiener(frameref,cp['wienernhood'])

        retval,frame16 = dfid.read() #TODO this is skipping every other frame!
        # TODO can we use dfid.set(cv.CV_CAP_PROP_POS_FRAMES,ifrm) to set 0-based index of next frame?
        rfi = ifrm
        if not retval:
            raise IOError('could not read video from {}'.format(dfid))

        if frame16.ndim>2:
            framegray = cv2.cvtColor(frame16, cv2.COLOR_RGB2GRAY)
        else:
            framegray = frame16 #copy NOT needed
    elif finf['reader'] == 'h5fm':   #one frame per file
        if ap['twoframe']:
            frameref = getfmradarframe(dfid[ifrm])[2]
            frameref = bytescale(frameref, ap['rawlim'][0], ap['rawlim'][1])
        frame16 = getfmradarframe(dfid[ifrm+1])[2]
        rfi = ifrm
        framegray = bytescale(frame16, ap['rawlim'][0], ap['rawlim'][1])
    elif finf['reader'] == 'h5vid':
        with h5py.File(str(dfid),'r',libver='latest') as f:
            if ap['twoframe']:
                frameref = bytescale(f['/rawimg'][ifrm,...],
                                     ap['rawlim'][0], ap['rawlim'][1])
                if dowiener:
                    frameref = wiener(frameref,cp['wienernhood'])

            #keep frame16 for histogram
            frame16 = f['/rawimg'][ifrm+1,...]
        framegray = bytescale(frame16,
                                 ap['rawlim'][0], ap['rawlim'][1])
        rfi = ifrm

    elif finf['reader'] == 'fits':
        #memmap = False required thru Astropy 1.1.1 due to BZERO used...
        #with fits.open(str(dfid),mode='readonly',memmap=False) as f:
        if ap['twoframe']:
            frameref = bytescale(dfid[0].data[ifrm,...],
                                 ap['rawlim'][0], ap['rawlim'][1])
            if dowiener:
                frameref = wiener(frameref,cp['wienernhood'])

        frame16 = dfid[0].data[ifrm+1,...]
        framegray = bytescale(frame16,
                                 ap['rawlim'][0], ap['rawlim'][1])

        rfi = ifrm #TODO: incorrect raw index with sequence of fits files
    else:
        raise TypeError('unknown reader type {}'.format(finf['reader']))


#%% current frame
    ap['rawframeind'][ifrm] = rfi

    if dowiener:
        framegray = wiener(framegray,cp['wienernhood'])

    if 'raw' in pshow:
        # cv2.imshow just divides by 256, NOT autoscaled!
        # http://docs.opencv.org/modules/highgui/doc/user_interface.html
        cv2.imshow('video', framegray)
#%% plotting
    if 'rawscaled' in pshow:
        cv2.imshow('raw video, scaled to 8-bit', framegray)
    # image histograms (to help verify proper scaling to uint8)
    if 'hist' in pshow:
        ax=figure().gca()
        hist(frame16.flatten(), bins=128, fc='w',ec='k', log=True)
        ax.set_title('raw uint16 values')

        ax=figure().gca()
        hist(framegray.flatten(), bins=128, fc='w',ec='k', log=True)
        ax.set_xlim((0,255))
        ax.set_title('normalized video into opt flow')

    if svh['video'] is not None:
        if savevideo == 'tif':
            svh['video'].save(framegray,compress=complvl)
        elif savevideo == 'vid':
            svh['video'].write(framegray)

    return framegray,frameref,ap
