// TMAP06 conversion-product acceptance probe for QuPath (default reader only).
//
// No provider is forced and no preference is changed: the server is built with
// the same call the GUI's open path uses (ImageServerProvider.buildServer).
//
// args: [0]=image path, [1]=output json, [2]=region PNG dir, [3]=project dir,
//       [4..]=fixed ROI specs "name,level,x,y,w,h" (level coordinates)
//
// Evidence written:
//   * default server identity, resolutions/levels, RGB/channel shape, mpp,
//     magnification (must be NaN mpp for TMAP06 — the header pixel size is not
//     adopted);
//   * fixed interior ROIs (tissue + block seam) as PNGs with their level box;
//   * TRUE outer-edge RegionRequests per level: a level-coordinate box that
//     ends at the level's last pixel, the full-resolution canvas outer edge,
//     the last pixel only, and a negative control that extends past the canvas
//     (recorded, never used as a pass condition);
//   * Bio-Formats layer-coordinate boundary reads (setResolution(level) +
//     openImage at the exact level box) — the independent check that the
//     product really contains the edge pixels QuPath's full-resolution
//     RegionRequest arithmetic may clip;
//   * project save + reopen (same default builder), lowest-level read, and the
//     first ROI re-read after reopen.
//
// check_tmap_acceptance.py compares every PNG with a tifffile decode of the
// same product and enforces the expected/actual geometry.
import qupath.lib.images.servers.ImageServerProvider
import qupath.lib.regions.RegionRequest
import qupath.lib.projects.Projects
import qupath.lib.projects.ProjectIO
import java.awt.image.BufferedImage
import javax.imageio.ImageIO
import com.google.gson.GsonBuilder

def path = args[0]
def outDir = new File(args[2]); outDir.mkdirs()
def projectDir = new File(args[3])
def out = [:]
out.qupath_version = qupath.lib.common.GeneralTools.getVersion()
out.bioformats_version = loci.formats.FormatTools.VERSION

def roiSpecs = []
for (int i = 4; i < args.length; i++) {
    def parts = args[i].split(',')
    assert parts.length == 6 : "ROI spec must be name,level,x,y,w,h: " + args[i]
    roiSpecs.add([name: parts[0], level: Integer.parseInt(parts[1]),
                  x: Integer.parseInt(parts[2]), y: Integer.parseInt(parts[3]),
                  w: Integer.parseInt(parts[4]), h: Integer.parseInt(parts[5])])
}

def json = new GsonBuilder().setPrettyPrinting()
    .serializeSpecialFloatingPointValues().create()

// 1) the DEFAULT server (same call as the GUI open path; no reader forced)
def server = ImageServerProvider.buildServer(path, BufferedImage.class)
try {
    def md = server.getMetadata()
    out.server = server.getClass().getName()
    out.width = server.getWidth(); out.height = server.getHeight()
    out.resolutions = server.nResolutions()
    out.downsamples = server.getPreferredDownsamples().toList()
    out.levels = (0..<server.nResolutions()).collect {
        [width: md.getLevel(it).getWidth(),
         height: md.getLevel(it).getHeight(),
         downsample: md.getLevel(it).getDownsample()]
    }
    out.is_rgb = server.isRGB()
    out.n_channels = server.nChannels()
    out.pixel_type = server.getPixelType().toString()
    out.channels = md.getChannels().collect { it.getName() }
    out.mpp_x = server.getPixelCalibration().getPixelWidthMicrons()
    out.mpp_y = server.getPixelCalibration().getPixelHeightMicrons()
    out.magnification = md.getMagnification()
    out.roi_reads = []
    out.edge_reads = []

    int fw = server.getWidth(), fh = server.getHeight()
    def doRead = { String name, int level, int fx, int fy, int fwReq, int fhReq ->
        double ds = md.getLevel(level).getDownsample()
        def req = RegionRequest.createInstance(server.getPath(), ds,
            fx, fy, fwReq, fhReq)
        def img = server.readRegion(req)
        def f = new File(outDir, name + '.png')
        ImageIO.write(img, 'png', f)
        return [name: name, level: level, downsample: ds,
                req_x: fx, req_y: fy, req_w: fwReq, req_h: fhReq,
                got_w: img.getWidth(), got_h: img.getHeight(),
                png: f.getName()]
    }

    // 1a) fixed interior ROIs (level coords -> exact full-res coords)
    for (roi in roiSpecs) {
        def lv = md.getLevel(roi.level)
        int ds = (int)Math.round(lv.getDownsample())
        out.roi_reads.add(doRead(roi.name, roi.level,
            roi.x * ds, roi.y * ds, roi.w * ds, roi.h * ds))
    }

    // 1b) true partial-edge requests
    for (int i = 0; i < server.nResolutions(); i++) {
        def lv = md.getLevel(i); double ds = lv.getDownsample()
        int lw = lv.getWidth(), lh = lv.getHeight()
        // last 80 LEVEL pixels expressed in full-resolution coordinates,
        // clamped to the canvas (this is the request that can clip)
        int fx = (int)Math.round((lw - 80) * ds)
        int fy = (int)Math.round((lh - 80) * ds)
        int fwr = (int)Math.min(Math.round(80 * ds), fw - fx)
        int fhr = (int)Math.min(Math.round(80 * ds), fh - fy)
        out.edge_reads.add(doRead("l${i}-edge-level-box", i, fx, fy, fwr, fhr))
        // the full-resolution canvas outer 80x80 box
        out.edge_reads.add(doRead("l${i}-canvas-edge", i, fw - 80, fh - 80, 80, 80))
        // the single last canvas pixel
        out.edge_reads.add(doRead("l${i}-last-pixel", i, fw - 1, fh - 1, 1, 1))
        // negative control: extends past the canvas (recorded only)
        out.edge_reads.add(doRead("l${i}-beyond-canvas", i, fw - 40, fh - 40, 80, 80))
    }
} finally { server.close() }

// 2) Bio-Formats resolution grouping + exact layer-coordinate boundary reads
def reader = new loci.formats.ImageReader()
def svc = new loci.common.services.ServiceFactory()
    .getInstance(loci.formats.services.OMEXMLService.class)
def meta = svc.createOMEXMLMetadata()
reader.setMetadataStore(meta)
reader.setFlattenedResolutions(false)
try {
    reader.setId(path)
    def bf = [reader: reader.getReader().getClass().getName(),
              series: reader.getSeriesCount(),
              resolutions: reader.getResolutionCount(),
              rgb: reader.isRGB(),
              rgb_channel_count: reader.getRGBChannelCount(),
              effective_size_c: reader.getEffectiveSizeC(),
              size_c: reader.getSizeC(),
              interleaved: reader.isInterleaved(),
              pixel_type: loci.formats.FormatTools.getPixelTypeString(
                  reader.getPixelType())]
    bf.resolution_sizes = (0..<reader.getResolutionCount()).collect { r ->
        reader.setResolution(r); [reader.getSizeX(), reader.getSizeY()]
    }
    reader.setResolution(0)
    bf.physical_size_x = meta.getPixelsPhysicalSizeX(0)?.value()?.doubleValue()
    bf.physical_size_y = meta.getPixelsPhysicalSizeY(0)?.value()?.doubleValue()
    bf.physical_unit = meta.getPixelsPhysicalSizeX(0)?.unit()?.getSymbol()
    bf.objective_nominal_magnification =
        meta.getInstrumentCount() > 0 && meta.getObjectiveCount(0) > 0 ?
            meta.getObjectiveNominalMagnification(0, 0) : null
    bf.channel_count = meta.getChannelCount(0)
    bf.channel_samples_per_pixel = (0..<meta.getChannelCount(0)).collect {
        meta.getChannelSamplesPerPixel(0, it)?.getValue()
    }
    out.bioformats = bf
    out.bf_reads = []
    def bfReader = new loci.formats.gui.BufferedImageReader(reader)
    for (int i = 0; i < reader.getResolutionCount(); i++) {
        reader.setResolution(i)
        int lw = reader.getSizeX(), lh = reader.getSizeY()
        // last 80 level pixels, exact level coordinates (no full-res mapping)
        int x = Math.max(0, lw - 80), y = Math.max(0, lh - 80)
        int w = Math.min(80, lw - x), h = Math.min(80, lh - y)
        def img = bfReader.openImage(0, x, y, w, h)
        def f = new File(outDir, "bf-l${i}-edge.png")
        ImageIO.write(img, 'png', f)
        out.bf_reads.add([level: i, x: x, y: y, w: w, h: h,
                          got_w: img.getWidth(), got_h: img.getHeight(),
                          png: f.getName()])
    }
} finally { reader.close() }

// 3) project save + reopen with the default builder
projectDir.mkdirs()
def project = Projects.createProject(projectDir, BufferedImage.class)
def s2 = ImageServerProvider.buildServer(path, BufferedImage.class)
try {
    def entry = project.addImage(s2.getBuilder())
    entry.setImageName('tmap acceptance sample')
    project.syncChanges()
} finally { s2.close() }
def restored = ProjectIO.loadProject(new File(project.getURI()), BufferedImage.class)
def reopened = restored.getImageList().get(0).getServerBuilder().build()
try {
    def lowest = reopened.nResolutions() - 1
    def req = RegionRequest.createInstance(reopened.getPath(),
        reopened.getDownsampleForResolution(lowest), 0, 0,
        reopened.getWidth(), reopened.getHeight())
    def img = reopened.readRegion(req)
    out.project_reopen = [server: reopened.getClass().getName(),
                          resolutions: reopened.nResolutions(),
                          width: reopened.getWidth(),
                          height: reopened.getHeight(),
                          mpp_x: reopened.getPixelCalibration().getPixelWidthMicrons(),
                          lowest_level_read: [img.getWidth(), img.getHeight()]]
    if (!roiSpecs.isEmpty()) {
        def roi = roiSpecs.get(0)
        def lv = reopened.getMetadata().getLevel(roi.level)
        int ds = (int)Math.round(lv.getDownsample())
        def rreq = RegionRequest.createInstance(reopened.getPath(),
            lv.getDownsample(), roi.x * ds, roi.y * ds, roi.w * ds, roi.h * ds)
        def rimg = reopened.readRegion(rreq)
        def f = new File(outDir, 'reopen-' + roi.name + '.png')
        ImageIO.write(rimg, 'png', f)
        out.project_reopen['roi_after_reopen'] = [
            name: roi.name, level: roi.level,
            got_w: rimg.getWidth(), got_h: rimg.getHeight(), png: f.getName()]
    }
} finally { reopened.close() }

new File(args[1]).text = json.toJson(out)
println('TMAP_ACCEPTANCE ' + json.toJson(out))
