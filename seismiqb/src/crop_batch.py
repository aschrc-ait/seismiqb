""" Seismic Crop Batch."""
import string
import random
from copy import copy

import numpy as np
import cv2
from scipy.signal import butter, lfilter, hilbert
from scipy.ndimage import gaussian_filter1d

from ..batchflow import FilesIndex, Batch, action, inbatch_parallel
from ..batchflow.batch_image import transform_actions # pylint: disable=no-name-in-module,import-error

from .horizon import Horizon
from .utils import aggregate
from .plotters import plot_image



AFFIX = '___'
SIZE_POSTFIX = 7
SIZE_SALT = len(AFFIX) + SIZE_POSTFIX



@transform_actions(prefix='_', suffix='_', wrapper='apply_transform')
class SeismicCropBatch(Batch):
    """ Batch with ability to generate 3d-crops of various shapes."""
    components = None

    def _init_component(self, *args, **kwargs):
        """ Create and preallocate a new attribute with the name ``dst`` if it
        does not exist and return batch indices."""
        _ = args
        dst = kwargs.get("dst")
        if dst is None:
            raise KeyError("dst argument must be specified")
        if isinstance(dst, str):
            dst = (dst,)
        for comp in dst:
            if not hasattr(self, comp):
                self.add_components(comp, np.array([np.nan] * len(self.index)))
        return self.indices


    @staticmethod
    def salt(path):
        """ Adds random postfix of predefined length to string.

        Parameters
        ----------
        path : str
            supplied string.

        Returns
        -------
        path : str
            supplied string with random postfix.
        Notes
        -----
        Action `crop` makes a new instance of SeismicCropBatch with
        different (enlarged) index. Items in that index should point to cube
        location to cut crops from. Since we can't store multiple copies of the same
        string in one index (due to internal usage of dictionary), we need to augment
        those strings with random postfix (which we can remove later).
        """
        chars = string.ascii_uppercase + string.digits
        return path + AFFIX + ''.join(random.choice(chars) for _ in range(SIZE_POSTFIX))

    @staticmethod
    def has_salt(path):
        """ Check whether path is salted. """
        return path[::-1].find(AFFIX) == SIZE_POSTFIX

    @staticmethod
    def unsalt(path):
        """ Removes postfix that was made by `salt` method.

        Parameters
        ----------
        path : str
            supplied string.

        Returns
        -------
        str
            string without postfix.
        """
        if AFFIX in path:
            return path[:-SIZE_SALT]
        return path


    def __getattr__(self, name):
        if hasattr(self.dataset, name):
            return getattr(self.dataset, name)
        return super().__getattr__(name)

    def get(self, item=None, component=None):
        """ Overload `get` in order to use it for some attributes (that are looking like geometries or labels). """
        if sum([attribute in component for attribute in ['label', 'geom']]):
            if isinstance(item, str) and self.has_salt(item):
                item = self.unsalt(item)
            res = getattr(self, component)
            if isinstance(res, dict) and item in res:
                return res[item]
            return res

        item = self.get_pos(None, component, item)
        return super().get(item, component)



    @action
    def crop(self, points, shape=None, loc=(0, 0, 0), side_view=False,
             adaptive_slices=False, grid_src='quality_grid', eps=3,
             dst='slices', passdown=None, dst_points='points', dst_shapes='shapes'):
        """ Generate positions of crops. Creates new instance of `SeismicCropBatch`
        with crop positions in one of the components (`slices` by default).

        Parameters
        ----------
        points : array-like
            Upper rightmost points for every crop and name of cube to
            cut it from. Order is: name, iline, xline, height. For example,
            ['Cube.sgy', 13, 500, 200] stands for crop has [13, 500, 200]
            as its upper rightmost point and must be cut from 'Cube.sgy' file.
        shape : sequence
            Desired shape of crops.
        loc : sequence of numbers
            Location of the point relative to the cut crop. Must be a location on unit cube.
        side_view : bool or float
            Determines whether to generate crops of transposed shape (xline, iline, height).
            If False, then shape is never transposed.
            If True, then shape is transposed with 0.5 probability.
            If float, then shape is transposed with that probability.
        adaptive_slices: bool or str
            If `adaptive`, then slices are created so that crops are cut only along the quality grid.
            If bool, then whether to make slices.
        dst : str, optional
            Component of batch to put positions of crops in.
        passdown : str of list of str
            Components of batch to keep in the new one.
        dst_points, dst_shapes : str
            Components to put point locations and crop shapes in.

        Notes
        -----
        Based on the first column of `points`, new instance of SeismicCropBatch is created.
        In order to keep multiple references to the same .sgy cube, each index is augmented
        with prefix of fixed length (check `salt` method for details).

        Returns
        -------
        SeismicCropBatch
            Batch with positions of crops in specified component.
        """
        # pylint: disable=protected-access

        if not hasattr(self, 'transformed'):
            new_index = [self.salt(ix) for ix in points[:, 0]]
            new_dict = {ix: self.index.get_fullpath(self.unsalt(ix))
                        for ix in new_index}
            new_batch = type(self)(FilesIndex.from_index(index=new_index, paths=new_dict, dirs=False))
            new_batch.transformed = True

            passdown = passdown or []
            passdown = [passdown] if isinstance(passdown, str) else passdown

            for component in passdown:
                if hasattr(self, component):
                    new_batch.add_components(component, getattr(self, component))

        else:
            if len(points) != len(self):
                raise ValueError('Subsequent usage of `crop` must have the same number of points!')
            new_batch = self

        if adaptive_slices:
            shape = np.asarray(shape)

            corrected_points_shapes = [self._correct_point_to_grid(point, shape, grid_src, eps) for point in points]
            points = [item[0] for item in corrected_points_shapes]
            shapes = [item[1] for item in corrected_points_shapes]
            new_batch.add_components((dst_points, dst_shapes), (points, shapes))

            slices = [self._make_slice(point, shape, loc) for point, shape in corrected_points_shapes]
            new_batch.add_components(dst, slices)
        else:
            shapes = self._make_shapes(points, shape, side_view)
            new_batch.add_components((dst_points, dst_shapes), (points, shapes))

            slices = [self._make_slice(point, shape, loc)
                      for point, shape in zip(points, shapes)]
            new_batch.add_components(dst, slices)
        return new_batch

    def _make_shapes(self, points, shape, side_view):
        """ Make an array of shapes to cut. """
        # If already array of desired shapes
        if isinstance(shape, np.ndarray) and shape.ndim == 2 and len(shape) == len(points):
            return shape

        if side_view:
            side_view = side_view if isinstance(side_view, float) else 0.5
        shape = np.asarray(shape)
        shapes = []
        for _ in points:
            if not side_view:
                shapes.append(shape)
            else:
                flag = np.random.random() > side_view
                if flag:
                    shapes.append(shape)
                else:
                    shapes.append(shape[[1, 0, 2]])
        shapes = np.array(shapes)
        return shapes


    def _make_slice(self, point, shape, loc=(0, 0, 0)):
        """ Creates list of `np.arange`'s for desired location. """
        if isinstance(point[1], float) or isinstance(point[2], float) or isinstance(point[3], float):
            ix = point[0]
            cube_shape = np.array(self.get(ix, 'geometries').cube_shape)
            slice_point = np.rint(point[1:].astype(float) * (cube_shape - np.array(shape))).astype(int)
        else:
            slice_point = point[1:]

        slice_ = []
        for i in range(3):
            start_point = int(max(slice_point[i] - loc[i]*shape[i], 0))
            end_point = start_point + shape[i]
            slice_.append(np.arange(start_point, end_point))
        return slice_

    def _correct_point_to_grid(self, point, shape, grid_src='quality_grid', eps=3):
        """ Move the point to the closest location in the quality grid. """
        #pylint: disable=too-many-return-statements
        ix = point[0]
        geometry = self.get(ix, 'geometries')
        grid = getattr(geometry, grid_src) if isinstance(grid_src, str) else grid_src
        shape_t = shape[[1, 0, 2]]

        pnt = (point[1:] * geometry.cube_shape)
        pnt = np.rint(pnt.astype(float)).astype(int)

        # Point is already in grid
        if grid[pnt[0], pnt[1]] == 1:
            sum_i = np.nansum(grid[pnt[0], max(pnt[1]-eps, 0) : pnt[1]+eps])
            sum_x = np.nansum(grid[max(pnt[0]-eps, 0) : pnt[0]+eps, pnt[1]])
            if sum_i >= sum_x:
                return point, shape
            return point, shape_t

        # Horizontal search: xline changes, shape is x-oriented
        for pnt_ in range(max(pnt[1]-eps, 0), min(pnt[1]+eps, geometry.cube_shape[1])):
            if grid[pnt[0], pnt_] == 1:
                sum_i = np.nansum(grid[pnt[0], max(pnt_-eps, 0):pnt_+eps])
                sum_x = np.nansum(grid[max(pnt[0]-eps, 0):pnt[0]+eps, pnt_])
                point[1:3] = np.array((pnt[0], pnt_)) / geometry.cube_shape[:2]
                if sum_i >= sum_x:
                    return point, shape
                return point, shape_t

        # Vertical search: inline changes, shape is i-oriented
        for pnt_ in range(max(pnt[0]-eps, 0), min(pnt[0]+eps, geometry.cube_shape[0])):
            if grid[pnt_, pnt[1]] == 1:
                sum_i = np.nansum(grid[pnt_, max(pnt[1]-eps, 0) : pnt[1]+eps])
                sum_x = np.nansum(grid[max(pnt_-eps, 0) : pnt_+eps, pnt[1]])
                point[1:3] = np.array((pnt_, pnt[1])) / geometry.cube_shape[:2]
                if sum_i >= sum_x:
                    return point, shape
                return point, shape_t

        # Double the search radius
        return self._correct_point_to_grid(point, shape, grid_src, 2*eps)



    @action
    @inbatch_parallel(init='indices', post='_assemble', target='for')
    def load_cubes(self, ix, dst, src='slices', **kwargs):
        """ Load data from cube in given positions.

        Parameters
        ----------
        src : str
            Component of batch with positions of crops to load.
        dst : str
            Component of batch to put loaded crops in.
        """
        #pylint: disable=unused-argument
        geom = self.get(ix, 'geometries')
        slice_ = self.get(ix, src)
        return geom.load_crop(slice_, **kwargs)


    @action
    @inbatch_parallel(init='indices', post='_assemble', target='for')
    def create_masks(self, ix, dst, src='slices', width=3, src_labels='labels', indices=-1):
        """ Create masks from labels-dictionary in given positions.

        Parameters
        ----------
        src : str
            Component of batch with positions of crops to load.
        dst : str
            Component of batch to put loaded masks in.
        width : int
            Width of horizons in the `horizon` mode.
        src_labels : str
            Component of batch with labels dict.
        indices : str, int or sequence of ints
            A choice scenario of used labels per crop.
            If -1 or 'all', all possible labels will be added.
            If 1 or 'single', one random label will be added.
            If array-like then elements are interpreted as indices of the desired labels
            and must be ints in range [0, len(horizons) - 1].
            Note if you want to pass an index of a single label it must be a list with one
            element.

        Returns
        -------
        SeismicCropBatch
            Batch with loaded masks in desired components.

        Notes
        -----
        Can be run only after labels-dict is loaded into labels-component.
        """
        #pylint: disable=unused-argument
        labels = self.get(ix, src_labels) if isinstance(src_labels, str) else src_labels
        labels = [labels] if not isinstance(labels, (tuple, list)) else labels
        check_sum = False

        if indices in [-1, 'all']:
            indices = np.arange(0, len(labels))
        elif indices in [1, 'single']:
            indices = np.arange(0, len(labels))
            np.random.shuffle(indices)
            check_sum = True
        elif isinstance(indices, int):
            raise ValueError('Inidices should be either -1, 1 or a sequence of ints.')
        elif isinstance(indices, (tuple, list, np.ndarray)):
            pass
        labels = [labels[idx] for idx in indices]

        slice_ = self.get(ix, src)
        shape_ = self.get(ix, 'shapes')
        mask = np.zeros((shape_), dtype='float32')

        for label in labels:
            mask = label.add_to_mask(mask, locations=slice_, width=width)
            if check_sum and np.sum(mask) > 0.0:
                break
        return mask


    @action
    @inbatch_parallel(init='indices', post='_post_mask_rebatch', target='for',
                      src='masks', threshold=0.8, passdown=None, axis=-1)
    def mask_rebatch(self, ix, src='masks', threshold=0.8, passdown=None, axis=-1):
        """ Remove elements with masks area lesser than a threshold.

        Parameters
        ----------
        threshold : float
            Minimum percentage of covered area (spatial-wise) for a mask to be kept in the batch.
        passdown : sequence of str
            Components to filter.
        axis : int
            Axis to project horizon to before computing mask area.
        """
        _ = threshold, passdown
        pos = self.get_pos(None, src, ix)
        mask = getattr(self, src)[pos]

        reduced = np.max(mask, axis=axis) > 0.0
        return np.sum(reduced) / np.prod(reduced.shape)

    def _post_mask_rebatch(self, areas, *args, src=None, passdown=None, threshold=None, **kwargs):
        #pylint: disable=protected-access, access-member-before-definition, attribute-defined-outside-init
        _ = args, kwargs
        new_index = [self.indices[i] for i, area in enumerate(areas) if area > threshold]
        new_dict = {idx: self.index._paths[idx] for idx in new_index}
        self.index = FilesIndex.from_index(index=new_index, paths=new_dict, dirs=False)

        passdown = passdown or []
        passdown.extend([src, 'slices'])
        passdown = list(set(passdown))

        for compo in passdown:
            new_data = [getattr(self, compo)[i] for i, area in enumerate(areas) if area > threshold]
            setattr(self, compo, np.array(new_data))
        return self


    @action
    @inbatch_parallel(init='_init_component', post='_assemble', target='for')
    def filter_out(self, ix, src=None, dst=None, mode=None, expr=None, low=None, high=None,
                   length=None, p=1.0):
        """ Zero out mask for horizon extension task.

        Parameters
        ----------
        src : str
            Component of batch with mask
        dst : str
            Component of batch to put cut mask in.
        mode : str
            Either point, line, iline or xline.
            If point, then only one point per horizon will be labeled.
            If iline or xline then single iline or xline with labeled.
            If line then randomly either single iline or xline will be
            labeled.
        expr : callable, optional.
            Some vectorized function. Accepts points in cube, returns either float.
            If not None, low or high/length should also be supplied.
        p : float
            Probability of applying the transform. Default is 1.
        """
        if not (src and dst):
            raise ValueError('Src and dst must be provided')

        pos = self.get_pos(None, src, ix)
        mask = getattr(self, src)[pos]
        coords = np.where(mask > 0)

        if np.random.binomial(1, 1 - p) or len(coords[0]) == 0:
            return mask
        if mode is not None:
            new_mask = np.zeros_like(mask)
            point = np.random.randint(len(coords))
            if mode == 'point':
                new_mask[coords[0][point], coords[1][point], :] = mask[coords[0][point], coords[1][point], :]
            elif mode == 'iline' or (mode == 'line' and np.random.binomial(1, 0.5)) == 1:
                new_mask[coords[0][point], :, :] = mask[coords[0][point], :, :]
            elif mode in ['xline', 'line']:
                new_mask[:, coords[1][point], :] = mask[:, coords[1][point], :]
            else:
                raise ValueError('Mode should be either `point`, `iline`, `xline` or `line')
        if expr is not None:
            coords = np.where(mask > 0)
            new_mask = np.zeros_like(mask)

            coords = np.array(coords).astype(np.float).T
            cond = np.ones(shape=coords.shape[0]).astype(bool)
            coords /= np.reshape(mask.shape, newshape=(1, 3))
            if low is not None:
                cond &= np.greater_equal(expr(coords), low)
            if high is not None:
                cond &= np.less_equal(expr(coords), high)
            if length is not None:
                low = 0 if not low else low
                cond &= np.less_equal(expr(coords), low + length)
            coords *= np.reshape(mask.shape, newshape=(1, 3))
            coords = np.round(coords).astype(np.int32)[cond]
            new_mask[coords[:, 0], coords[:, 1], coords[:, 2]] = mask[coords[:, 0],
                                                                      coords[:, 1],
                                                                      coords[:, 2]]
        else:
            new_mask = mask
        return new_mask


    @action
    @inbatch_parallel(init='indices', post='_assemble', target='for')
    def scale(self, ix, mode, src=None, dst=None):
        """ Scale values in crop. """
        #pylint: disable=unused-argument
        pos = self.get_pos(None, src, ix)
        comp_data = getattr(self, src)[pos]
        geom = self.get(ix, 'geometries')
        return geom.scaler(comp_data, mode)


    @action
    @inbatch_parallel(init='_init_component', post='_assemble', target='for')
    def concat_components(self, ix, src, dst, axis=-1):
        """ Concatenate a list of components and save results to `dst` component.

        Parameters
        ----------
        src : array-like
            List of components to concatenate of length more than one.
        dst : str
            Component of batch to put results in.
        axis : int
            The axis along which the arrays will be joined.
        """
        _ = dst
        if not isinstance(src, (list, tuple, np.ndarray)) or len(src) < 2:
            raise ValueError('Src must contain at least two components to concatenate')
        result = []
        for component in src:
            pos = self.get_pos(None, component, ix)
            result.append(getattr(self, component)[pos])
        return np.concatenate(result, axis=axis)

    @action
    @inbatch_parallel(init='indices', target='for', post='_masks_to_horizons_post')
    def masks_to_horizons(self, ix, src='masks', src_slices='slices', dst='predicted_labels', prefix='predict',
                          threshold=0.5, averaging='mean', minsize=0, order=(2, 0, 1), skip_merge=False,
                          mean_threshold=2.0, adjacency=1):
        """ Convert labels from horizons-mask into point-cloud format. Fetches point-clouds from
        a batch of masks, then merges resulting clouds to those stored in `dst`, whenever possible.

        Parameters
        ----------
        src_masks : str
            component of batch that stores masks.
        src_slices : str
            component of batch that stores slices of crops.
        dst : str/object
            component of batch to store the resulting labels, o/w a storing object.
        threshold : float
            parameter of mask-thresholding.
        averaging : str
            method of pandas.groupby used for finding the center of a horizon.
        coordinates : str
            coordinates-mode to use for keys of point-cloud. Can be either 'cubic'
            or 'lines'. In case of `lines`-option, `geometries` must be loaded as
            a component of batch.
        order : tuple of int
            axes-param for `transpose`-operation, applied to a mask before fetching point clouds.
            Default value of (2, 0, 1) is applicable to standart pipeline with one `rotate_axes`
            applied to images-tensor.
        mean_threshold : int
            if adjacent horizons do not diverge for more than this distance, they can be merged together.
        adjacency : int
            max distance between a pair of horizon-borders when the horizons can be adjacent.
        Returns
        -------
        SeismicCropBatch
            batch with fetched labels.
        """
        _ = dst, mean_threshold, adjacency, skip_merge

        # threshold the mask, reshape and rotate the mask if needed
        pos = self.get_pos(None, src, ix)
        mask = getattr(self, src)[pos]
        if np.array(order).reshape(-1, 3).shape[0] > 0:
            order = order[pos]
        mask = np.transpose(mask, axes=order)

        #
        geom = self.get(ix, 'geometries')
        grid_info = {
            'geom': geom,
            'range': [[self.get(ix, src_slices)[k][0], None] for k in range(3)]
        }

        # get horizons and merge them with matching aggregated ones
        horizons = Horizon.from_mask(mask, grid_info, threshold=threshold,
                                     averaging=averaging, minsize=minsize, prefix=prefix)
        return horizons


    def _masks_to_horizons_post(self, horizons_lists, *args, dst=None, skip_merge=False,
                                mean_threshold=2.0, adjacency=1, **kwargs):
        """ Stitch a set of point-clouds to a point cloud form dst if possible.
        Post for `get_point_cloud`-action.
        """
        _, _ = args, kwargs
        if dst is None:
            raise ValueError("dst should be initialized with empty list.")

        if skip_merge:
            setattr(self, dst, [hor for hor_list in horizons_lists for hor in hor_list])
            return self

        # remember, horizons_lists contains lists of horizons
        for horizons in horizons_lists:
            for horizon_candidate in horizons:
                for horizon_target in dst:
                    merge_code, _ = Horizon.verify_merge(horizon_target, horizon_candidate,
                                                         mean_threshold=mean_threshold,
                                                         adjacency=adjacency)

                    if merge_code == 3:
                        merged = Horizon.overlap_merge(horizon_target, horizon_candidate, inplace=True)
                    elif merge_code == 2:
                        merged = Horizon.adjacent_merge(horizon_target, horizon_candidate, inplace=True,
                                                        adjacency=adjacency, mean_threshold=mean_threshold)
                    else:
                        merged = False
                    if merged:
                        break
                else:
                    # if a horizon cannot be stitched to a horizon from dst, we enrich dst with it
                    dst.append(horizon_candidate)
        return self



    @action
    @inbatch_parallel(init='run_once', target='for')
    def assemble_crops(self, src, dst, grid_info, order=None):
        """ Glue crops together in accordance to the grid.

        Note
        ----
        In order to use this action you must first call `make_grid` method of SeismicCubeset.

        Parameters
        ----------
        src : array-like
            Sequence of crops.
        dst : str
            Component of batch to put results in.
        grid_info : dict
            Dictionary with information about grid. Should be created by `make_grid` method.

        Returns
        -------
        SeismicCropBatch
            Batch with assembled subcube in desired component.
        """
        # Do nothing until there is a crop for every point
        if len(src) != len(grid_info['grid_array']):
            return self

        order = order or (2, 0, 1)
        # Since we know that cube is 3-d entity, we can get rid of
        # unneccessary dimensions
        src = np.array(src)
        src = src if len(src.shape) == 4 else np.squeeze(src, axis=-1)
        assembled = aggregate(src, grid_info['grid_array'], grid_info['crop_shape'],
                              grid_info['predict_shape'], order)

        setattr(self, dst, assembled)
        return self


    def _adaptive_reshape_(self, crop, shape):
        """ Changes axis of view to match desired shape.
        Must be used in combination with `side_view` argument of `crop` action.

        Parameters
        ----------
        shape : sequence
            Desired shape of resulting crops.
        """
        if (np.array(crop.shape) != np.array(shape)).any():
            return crop.transpose([1, 0, 2])
        return crop

    def _shift_masks_(self, crop, n_segments=3, max_shift=4, max_len=10):
        """ Randomly shift parts of the crop up or down.

        Parameters
        ----------
        n_segments : int
            Number of segments to shift.
        max_shift : int
            Size of shift along vertical axis.
        max_len : int
            Size of shift along horizontal axis.
        """
        crop = np.copy(crop)
        for _ in range(n_segments):
            # Point of starting the distortion, its length and size
            begin = np.random.randint(0, crop.shape[1])
            length = np.random.randint(5, max_len)
            shift = np.random.randint(-max_shift, max_shift)

            # Apply shift
            segment = crop[:, begin:min(begin + length, crop.shape[1]), :]
            shifted_segment = np.zeros_like(segment)
            if shift > 0:
                shifted_segment[:, :, shift:] = segment[:, :, :-shift]
            elif shift < 0:
                shifted_segment[:, :, :shift] = segment[:, :, -shift:]
            crop[:, begin:min(begin + length, crop.shape[1]), :] = shifted_segment
        return crop

    def _bend_masks_(self, crop, angle=10):
        """ Rotate part of the mask on a given angle.
        Must be used for crops in (xlines, heights, inlines) format.
        """
        shape = crop.shape

        if np.random.random() >= 0.5:
            point_x = np.random.randint(shape[0]//2, shape[0])
            point_h = np.argmax(crop[point_x, :, :])

            if np.sum(crop[point_x, point_h, :]) == 0.0:
                return np.copy(crop)

            matrix = cv2.getRotationMatrix2D((point_h, point_x), angle, 1)
            rotated = cv2.warpAffine(crop, matrix, (shape[1], shape[0])).reshape(shape)

            combined = np.zeros_like(crop)
            combined[:point_x, :, :] = crop[:point_x, :, :]
            combined[point_x:, :, :] = rotated[point_x:, :, :]
        else:
            point_x = np.random.randint(0, shape[0]//2)
            point_h = np.argmax(crop[point_x, :, :])

            if np.sum(crop[point_x, point_h, :]) == 0.0:
                return np.copy(crop)

            matrix = cv2.getRotationMatrix2D((point_h, point_x), angle, 1)
            rotated = cv2.warpAffine(crop, matrix, (shape[1], shape[0])).reshape(shape)

            combined = np.zeros_like(crop)
            combined[point_x:, :, :] = crop[point_x:, :, :]
            combined[:point_x, :, :] = rotated[:point_x, :, :]
        return combined


    def _transpose_(self, crop, order):
        """ Change order of axis. """
        return np.transpose(crop, order)

    def _rotate_axes_(self, crop):
        """ The last shall be first and the first last.

        Notes
        -----
        Actions `crop`, `load_cubes`, `create_mask` make data in [iline, xline, height]
        format. Since most of the models percieve ilines as channels, it might be convinient
        to change format to [xlines, height, ilines] via this action.
        """
        crop_ = np.swapaxes(crop, 0, 1)
        crop_ = np.swapaxes(crop_, 1, 2)
        return crop_

    def _add_axis_(self, crop):
        """ Add new axis.

        Notes
        -----
        Used in combination with `dice` and `ce` losses to tell model that input is
        3D entity, but 2D convolutions are used.
        """
        return crop[..., np.newaxis]

    def _additive_noise_(self, crop, scale):
        """ Add random value to each entry of crop. Added values are centered at 0.

        Parameters
        ----------
        scale : float
            Standart deviation of normal distribution.
        """
        rng = np.random.default_rng()
        noise = scale * rng.standard_normal(dtype=np.float32, size=crop.shape)
        return crop + noise

    def _multiplicative_noise_(self, crop, scale):
        """ Multiply each entry of crop by random value, centered at 1.

        Parameters
        ----------
        scale : float
            Standart deviation of normal distribution.
        """
        rng = np.random.default_rng()
        noise = 1 + scale * rng.standard_normal(dtype=np.float32, size=crop.shape)
        return crop * noise

    def _cutout_2d_(self, crop, patch_shape, n):
        """ Change patches of data to zeros.

        Parameters
        ----------
        patch_shape : array-like
            Shape or patches along each axis.
        n : float
            Number of patches to cut.
        """
        rnd = np.random.RandomState(int(n*100)).uniform
        patch_shape = patch_shape.astype(int)

        copy_ = copy(crop)
        for _ in range(int(n)):
            x_ = int(rnd(max(crop.shape[0] - patch_shape[0], 1)))
            h_ = int(rnd(max(crop.shape[1] - patch_shape[1], 1)))
            copy_[x_:x_+patch_shape[0], h_:h_+patch_shape[1], :] = 0
        return copy_

    def _rotate_(self, crop, angle):
        """ Rotate crop along the first two axes.

        Parameters
        ----------
        angle : float
            Angle of rotation.
        """
        shape = crop.shape
        matrix = cv2.getRotationMatrix2D((shape[1]//2, shape[0]//2), angle, 1)
        return cv2.warpAffine(crop, matrix, (shape[1], shape[0])).reshape(shape)

    def _flip_(self, crop, axis=0, seed=0.1, threshold=0.5):
        """ Flip crop along the given axis.

        Parameters
        ----------
        axis : int
            Axis to flip along
        """
        rnd = np.random.RandomState(int(seed*100)).uniform
        if rnd() >= threshold:
            return cv2.flip(crop, axis).reshape(crop.shape)
        return crop

    def _scale_2d_(self, crop, scale):
        """ Zoom in or zoom out along the first two axes of crop.

        Parameters
        ----------
        scale : float
            Zooming factor.
        """
        shape = crop.shape
        matrix = cv2.getRotationMatrix2D((shape[1]//2, shape[0]//2), 0, scale)
        return cv2.warpAffine(crop, matrix, (shape[1], shape[0])).reshape(shape)

    def _affine_transform_(self, crop, alpha_affine=10):
        """ Perspective transform. Moves three points to other locations.
        Guaranteed not to flip image or scale it more than 2 times.

        Parameters
        ----------
        alpha_affine : float
            Maximum distance along each axis between points before and after transform.
        """
        rnd = np.random.RandomState(int(alpha_affine*100)).uniform
        shape = np.array(crop.shape)[:2]
        if alpha_affine >= min(shape)//16:
            alpha_affine = min(shape)//16

        center_ = shape // 2
        square_size = min(shape) // 3

        pts1 = np.float32([center_ + square_size,
                           center_ - square_size,
                           [center_[0] + square_size, center_[1] - square_size]])

        pts2 = pts1 + rnd(-alpha_affine, alpha_affine, size=pts1.shape).astype(np.float32)


        matrix = cv2.getAffineTransform(pts1, pts2)
        return cv2.warpAffine(crop, matrix, (shape[1], shape[0])).reshape(crop.shape)

    def _perspective_transform_(self, crop, alpha_persp):
        """ Perspective transform. Moves four points to other four.
        Guaranteed not to flip image or scale it more than 2 times.

        Parameters
        ----------
        alpha_persp : float
            Maximum distance along each axis between points before and after transform.
        """
        rnd = np.random.RandomState(int(alpha_persp*100)).uniform
        shape = np.array(crop.shape)[:2]
        if alpha_persp >= min(shape) // 16:
            alpha_persp = min(shape) // 16

        center_ = shape // 2
        square_size = min(shape) // 3

        pts1 = np.float32([center_ + square_size,
                           center_ - square_size,
                           [center_[0] + square_size, center_[1] - square_size],
                           [center_[0] - square_size, center_[1] + square_size]])

        pts2 = pts1 + rnd(-alpha_persp, alpha_persp, size=pts1.shape).astype(np.float32)

        matrix = cv2.getPerspectiveTransform(pts1, pts2)
        return cv2.warpPerspective(crop, matrix, (shape[1], shape[0])).reshape(crop.shape)

    def _elastic_transform_(self, crop, alpha=40, sigma=4):
        """ Transform indexing grid of the first two axes.

        Parameters
        ----------
        alpha : float
            Maximum shift along each axis.
        sigma : float
            Smoothening factor.
        """
        rng = np.random.default_rng(seed=int(alpha*100))
        shape_size = crop.shape[:2]

        grid_scale = 4
        alpha //= grid_scale
        sigma //= grid_scale
        grid_shape = (shape_size[0]//grid_scale, shape_size[1]//grid_scale)

        blur_size = int(4 * sigma) | 1
        rand_x = cv2.GaussianBlur(rng.random(size=grid_shape, dtype=np.float32) * 2 - 1,
                                  ksize=(blur_size, blur_size), sigmaX=sigma) * alpha
        rand_y = cv2.GaussianBlur(rng.random(size=grid_shape, dtype=np.float32) * 2 - 1,
                                  ksize=(blur_size, blur_size), sigmaX=sigma) * alpha
        if grid_scale > 1:
            rand_x = cv2.resize(rand_x, shape_size[::-1])
            rand_y = cv2.resize(rand_y, shape_size[::-1])

        grid_x, grid_y = np.meshgrid(np.arange(shape_size[1]), np.arange(shape_size[0]))
        grid_x = (grid_x.astype(np.float32) + rand_x)
        grid_y = (grid_y.astype(np.float32) + rand_y)

        distorted_img = cv2.remap(crop, grid_x, grid_y,
                                  borderMode=cv2.BORDER_REFLECT_101,
                                  interpolation=cv2.INTER_LINEAR)
        return distorted_img.reshape(crop.shape)

    def _bandwidth_filter_(self, crop, lowcut=None, highcut=None, fs=1, order=3):
        """ Keep only frequences between lowcut and highcut.

        Notes
        -----
        Use it before other augmentations, especially before ones that add lots of zeros.

        Parameters
        ----------
        lowcut : float
            Lower bound for frequences kept.
        highcut : float
            Upper bound for frequences kept.
        fs : float
            Sampling rate.
        order : int
            Filtering order.
        """
        nyq = 0.5 * fs
        if lowcut is None:
            b, a = butter(order, highcut / nyq, btype='high')
        elif highcut is None:
            b, a = butter(order, lowcut / nyq, btype='low')
        else:
            b, a = butter(order, [lowcut / nyq, highcut / nyq], btype='band')
        return lfilter(b, a, crop, axis=1)

    def _sign_(self, crop):
        """ Element-wise indication of the sign of a number. """
        return np.sign(crop)

    def _analytic_transform_(self, crop, axis=1, mode='phase'):
        """ Compute instantaneous phase or frequency via the Hilbert transform.

        Parameters
        ----------
        axis : int
            Axis of transformation. Intended to be used after `rotate_axes`, so default value
            is to make transform along depth dimension.
        mode : str
            If 'phase', compute instantaneous phase.
            If 'freq', compute instantaneous frequency.
        """
        analytic = hilbert(crop, axis=axis)
        phase = np.unwrap(np.angle(analytic))

        if mode == 'phase':
            return phase
        if 'freq' in mode:
            return np.diff(phase, axis=axis, prepend=0) / (2*np.pi)
        raise ValueError('Unknown `mode` parameter.')


    def _gaussian_filter_(self, crop, axis=1, sigma=2, order=0):
        """ Apply a gaussian filter along specified axis. """
        return gaussian_filter1d(crop, sigma=sigma, axis=axis, order=order)


    def plot_components(self, *components, idx=0, mode='overlap', order_axes=None, **kwargs):
        """ Plot components of batch.

        Parameters
        ----------
        idx : int or None
            If int, then index of desired image in list.
            If None, then no indexing is applied.
        components : str or sequence of str
            Components to get from batch and draw.
        plot_mode : bool
            If 'overlap', then images are drawn one over the other with transparency.
            If 'separate', then images are drawn on separate layouts.
        order_axes : sequence of int
            Determines desired order of the axis. The first two are plotted.
        """
        if idx is not None:
            imgs = [getattr(self, comp)[idx] for comp in components]
        else:
            imgs = [getattr(self, comp) for comp in components]

        # set some defaults
        kwargs = {
            'label': 'Batch components',
            'titles': components,
            'xlabel': 'xlines',
            'ylabel': 'depth',
            'cmap': ['gray'] + ['viridis']*len(components) if mode == 'separate' else 'gray',
            **kwargs
        }

        plot_image(imgs, mode=mode, order_axes=order_axes, **kwargs)
