#!/usr/bin/python



# Import relevant packages:
import glob
import sys
import os
from netCDF4 import Dataset
import numpy as np
import argparse
import datetime as dt
from dateutil import rrule as rr
from dateutil.relativedelta import relativedelta as rd
import matplotlib.pyplot as plt
from scipy import stats

# Silly import hack for ALICE
sys.path.append(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        '..'))
from uptrop.read_pandora import read_pandora
from uptrop.bootstrap import rma
from uptrop.constants import DU_TO_MOLECULES_PER_CM2 as du2moleccm2
from uptrop.constants import MOLES_PER_M2_TO_MOLECULES_PER_CM2 as molm2_to_moleccm2


# Turn off warnings:
#np.warnings.filterwarnings('ignore')


class NoDataException(Exception):
    pass


class UnequalFileException(Exception):
    pass


class BadNo2ColException(Exception):
    pass


class BadCloudShapeException(Exception):
    pass


class InvalidCloudProductException(Exception):
    pass


class NoPandoraException(Exception):
    pass


class DataCollector:
    """Collates tropomi and pandora data for a region around a Pandora site"""
    def __init__(self, start_date, end_date, pan_ver):
        """Creates a collator between two dates.

        :param start_date: The start date (inclusive)
        :type start_date: DateTime
        :param end_date: The end date (inclusive)
        :type end_date: DateTime
        """
        # Define final array of coincident data for each day at Pandora site:
        self.start_date = start_date
        self.end_date = end_date
        self.pan_ver = pan_ver
        nvals = get_days_since_data_start(end_date, start_date) + 1
        self.pan_no2 = np.zeros(nvals)
        self.s5p_no2 = np.zeros(nvals)
        self.s5p_ch = np.zeros(nvals)
        self.s5p_cf = np.zeros(nvals)
        self.pan_wgt = np.zeros(nvals)
        self.s5p_wgt = np.zeros(nvals)
        self.pan_cnt = np.zeros(nvals)
        self.pan_err = np.zeros(nvals)
        self.start_utc = np.zeros(nvals)
        self.end_utc = np.zeros(nvals)
        self.start_utc[:] = np.nan
        self.end_utc[:] = np.nan
        self.s5p_cnt = np.zeros(nvals)

        self.n_days = nvals

    def add_trop_data_to_day(self, date, trop_data):
        """Adds the tropomi gc_data, gc_data error, cloud pressure and cloud fraction to a date in this object
        Call set_trop_ind_for_day before this function

        :param date: The date to add the data to.
        :type date: DateTime
        :param trop_data: The tropomi data on a day
        :type trop_date: TropomiData
        """

        tomiind = self.tomiind
        day_index = get_days_since_data_start(date, self.start_date)

        # Add TROPOMI total NO2 to final array of daily means:
        self.s5p_no2[day_index] += sum(np.divide(trop_data.no2val[tomiind], np.square(trop_data.no2err[tomiind])))
        self.s5p_wgt[day_index] += sum(np.divide(1.0, np.square(trop_data.no2err[tomiind])))
        self.s5p_ch[day_index] += sum(trop_data.cldpres[tomiind] * 1e-2)
        self.s5p_cf[day_index] += sum(trop_data.cldfrac[tomiind])
        self.s5p_cnt[day_index] += len(tomiind)

    def set_trop_ind_for_day(self, date, diff_deg, trop_data, pandora_data):
        """Sets tomiind (the index for processing) for a date and area around a pandora site

        :param date: The date of data to find
        :type date: DateTime
        :param diff_deg: The size of the grid square over the Pandora site to extract Tropomi data from
        :type grid_square: float
        :param trop_data: The CloudComparisonData object containing tropomi data
        :type trop_data: TropomiData
        :param pandora_data: The PandoraData object containining Pandora data for date
        :type pandora_data: PandoraData
        :raises NoDataException: Raised if there is no tropomi data for date"""
        # Find coincident data for this file:
        self.difflon = abs(np.subtract(trop_data.lons, pandora_data.panlon))
        self.difflat = abs(np.subtract(trop_data.lats, pandora_data.panlat))


        # Use distanc (degrees) to find coincident data.
        # For Pandora 'Trop' data, only consider TROPOMI scenes where the
        # total column exceeds the stratospheric column:
        if (trop_data.no2_col == 'Tot'):
            tomiind = np.argwhere((self.difflon <= diff_deg)
                                  & (self.difflat <= diff_deg)
                                  & (trop_data.no2val != np.nan)
                                  & (trop_data.omi_dd == date.day))
        if (trop_data.no2_col == 'Trop'):
            tomiind = np.argwhere((self.difflon <= diff_deg)
                                  & (self.difflat <= diff_deg)
                                  & (trop_data.no2val != np.nan)
                                  & (trop_data.omi_dd == date.day)
                                  & (trop_data.no2val > 2e13))
                                  #& (trop_data.stratcol < trop_data.totcol))
      
        # Skip if no data:
        if (len(tomiind) == 0):
            raise NoDataException
        self.tomiind = tomiind
      
        # Get min and max TROPOMI UTC for this orbit:
        # Choose min and max time window of TROPOMI 0.2 degrees
        # around Pandora site:
        minhh = np.nanmin(trop_data.omi_utc_hh[tomiind])
        maxhh = np.nanmax(trop_data.omi_utc_hh[tomiind])
        mintime = np.nanmin(trop_data.tomi_hhmm[tomiind])
        maxtime = np.nanmax(trop_data.tomi_hhmm[tomiind])
        if (minhh == maxhh):
            self.hhsite = [mintime]
        else:
            self.hhsite = [mintime, maxtime]
        self.nhrs = len(self.hhsite)

    def add_pandora_data_to_day(self, date, hour, diff_hh, pandora_data):
        """Adds pandora-measured NO2 and error on date at hour to collector
        Pandora flag threshold selected is from https://www.atmos-meas-tech.net/13/205/2020/amt-13-205-2020.pdf
        NO2 and error are converted from DU to molec/cm2

        :param date: The date to search in the pandora data for
        :type date: DateTime
        :param hour: The hour of the Tropomi overpass
        :type hour: float
        :param diff_hh: The range around hour to sample
        :type diff_hh: float
        :param pandora_data: The PandoraData object containing the data for date and hour
        :type pandora_data: PandoraData
        """
        # Find relevant Pandora data for this year, month and day:
        # Pandora flag threshold selected is from https://www.atmos-meas-tech.net/13/205/2020/amt-13-205-2020.pdf
        panind = np.argwhere((pandora_data.panyy == date.year)
                             & (pandora_data.panmon == date.month)
                             & (pandora_data.pandd == date.day)
                             & (pandora_data.panno2 > -9e99)
                             & (pandora_data.panqaflag <= 11)
                             & (pandora_data.panqaflag != 2)
                             & (pandora_data.pan_hhmm >= self.hhsite[hour] - diff_hh)
                             & (pandora_data.pan_hhmm <= self.hhsite[hour] + diff_hh))
        # Proceed if there are Pandora data points:
        if len(panind) == 0:
            print("No pandora data for day {}".format(date))
            raise NoPandoraException
        
        # Create arrays of relevant data and convert from DU to molec/cm2
        # for version 1.7 or moles/m2 to molec/cm2 for version 1.8:
        if (self.pan_ver=='1.7'):
            tno2 = np.multiply(pandora_data.panno2[panind], du2moleccm2)
            tunc = np.multiply(pandora_data.panno2err[panind], du2moleccm2)
        else:
            tno2 = np.multiply(pandora_data.panno2[panind], molm2_to_moleccm2)
            tunc = np.multiply(pandora_data.panno2err[panind], molm2_to_moleccm2)
        #tqa = pandora_data.panqaflag[panind]
        # get day of year:
        day_of_year = get_days_since_data_start(date, self.start_date)

        # Get min and max time used to cosample Pandora and TROPOMI:
        min_utc = min(pandora_data.pan_hhmm[panind]) 
        max_utc = max(pandora_data.pan_hhmm[panind]) 
        if np.isnan(self.start_utc[day_of_year]):
            self.start_utc[day_of_year] = min_utc 
            self.end_utc[day_of_year] = max_utc
        if ( ~np.isnan(self.start_utc[day_of_year]) and \
             min_utc < self.start_utc[day_of_year] ):
            self.start_utc[day_of_year] = min_utc 
        if ( ~np.isnan(self.end_utc[day_of_year]) and \
             max_utc > self.end_utc[day_of_year] ):
            self.end_utc[day_of_year] = max_utc

        # Add Pandora total NO2 to final array:
        for w in range(len(panind)):
            terr = np.divide(1.0, np.square(tunc[w]))
            twgt = terr
            if (trop_data.no2_col=='Trop'): twgt = 1.0
            self.pan_no2[day_of_year] += np.multiply(tno2[w], twgt)
            self.pan_wgt[day_of_year] += twgt
            self.pan_err[day_of_year] += terr
            self.pan_cnt[day_of_year] += len(panind)

    def apply_weight_to_means(self):
        """Applies weighting to every aggregated variable. Call at end of processing."""
        # Get daily error-weighted means:
        self.pan_no2 = self.pan_no2 / self.pan_wgt
        self.pan_err = np.divide(1, np.sqrt(self.pan_err))
        self.s5p_no2 = self.s5p_no2 / self.s5p_wgt
        self.s5p_ch = self.s5p_ch / self.s5p_cnt
        self.s5p_cf = self.s5p_cf / self.s5p_cnt
        self.s5p_wgt = np.divide(1, np.sqrt(self.s5p_wgt))
        print('Min & max relative errors (Pandora): ', np.nanmin(np.divide(self.pan_err, self.pan_no2)),
              np.nanmax(np.divide(self.pan_err, self.pan_no2)))
        print('Min & max relative errors (TROPOMI): ', np.nanmin(np.divide(self.s5p_wgt, self.s5p_no2)),
              np.nanmax(np.divide(self.s5p_wgt, self.s5p_no2)))

    def plot_data(self, PANDORA_SITE):
        """Time series of daily means"""
        # Plot time series:
        plt.figure(1, figsize=(10, 5))
        x = np.arange(0, self.n_days, 1)
        plt.errorbar(x, self.pan_no2 * 1e-14, yerr=self.pan_err * 1e-14,
                     fmt='.k', color='black', capsize=5, capthick=2,
                     ecolor='black', markersize=12, label='Pandora')
        plt.errorbar(x, self.s5p_no2* 1e-14, yerr=self.s5p_wgt * 1e-14,
                     fmt='.k', color='blue', capsize=5, capthick=2,
                     ecolor='blue', markeredgecolor='blue',
                     markerfacecolor='blue', markersize=12, label='TROPOMI')
        plt.ylim(Y_MIN, Y_MAX)
        plt.xlabel('Days since 1 June 2019')
        plt.ylabel('$NO_2$ total VCD [$10^{14}$ molecules $cm^2$]')
        leg = plt.legend(loc='lower left', fontsize='large')
        leg.get_frame().set_linewidth(0.0)
        #plt.savefig('./Images/tropomi-'+PANDORA_SITE+
        #            '-pandora-gc_data-timeseries-v1-dec2018-may2021.ps',
        #            format='ps',transparent=True,bbox_inches='tight',dpi=100)
        # Plot scatterplot:
        tx = self.pan_no2
        ty = self.s5p_no2
        nas = np.logical_or(np.isnan(tx), np.isnan(ty))
        print('No. of coincident points = ', len(tx[~nas]))
        r = stats.pearsonr(tx[~nas], ty[~nas])
        print('Correlation: ', r[0])
        # Get mean difference:
        Diff = np.subtract(np.mean(ty[~nas]), np.mean(tx[~nas]))
        print('TROPOMI minus Pandora (10^14) = ', Diff * 1e-14)
        NMB = 100. * np.divide(Diff, np.mean(tx[~nas]))
        print('TROPOMI NMB (%) = ', NMB)
        # RMA regression:
        result = rma(tx[~nas] * 1e-14, ty[~nas] * 1e-14, len(tx[~nas]), 10000)
        print('Intercept (10^14): ', result[1])
        print('Slope: ', result[0],flush=True)
        fig = plt.figure(2)
        plt.figure(2, figsize=(6, 5))
        ax = fig.add_subplot(1, 1, 1)
        plt.plot(1e-14 * tx, 1e-14 * ty, 'o', color='black')
        plt.xlim(0, 60)
        plt.ylim(0, 60)
        plt.xlabel('Pandora $NO_2$ total VCD [$10^{14}$ molecules $cm^2$]')
        plt.ylabel('TROPOMI $NO_2$ total VCD [$10^{14}$ molecules $cm^2$]')
        xvals = np.arange(0, 60, 2)
        yvals = result[1] + xvals * result[0]
        plt.plot(xvals, yvals, '-')
        add2plt = ("y = {a:.3f}x + {b:.3f}".
                   format(a=result[0], b=result[1]))
        plt.text(0.1, 0.9, add2plt, fontsize=10,
                 ha='left', va='center', transform=ax.transAxes)
        add2plt = ("r = {a:.3f}".format(a=r[0]))
        plt.text(0.1, 0.84, add2plt, fontsize=10,
                 ha='left', va='center', transform=ax.transAxes)
        #plt.savefig('./Images/v020101/tropomi-'+PANDORA_SITE+
        #            '-pandora-gc_data-scatterplot-v1-jun2019-apr2020.ps',
        #            format='ps',transparent=True,bbox_inches='tight',dpi=100)
        plt.show()

    def write_to_netcdf(self, file):
        """Saves aggregated data to netcdf"""
        # Save the data to NetCDF:
        ncout = Dataset(file, mode='w', format='NETCDF4')
        # Set array sizes:
        TDim = self.n_days
        ncout.createDimension('time', TDim)
        # create days axis
        days = ncout.createVariable('days', np.float32, ('time',))
        days.units = 'days since 2019-06-01'
        days.long_name = 'days in days since 2019-06-01'
        days[:] = np.arange(0, self.n_days, 1)

        start_utc = ncout.createVariable('start_utc', np.float32, ('time',))
        start_utc.units = 'unitless'
        start_utc.long_name = 'Start UTC hour of coincident TROPOMI and Pandorra sampling window'
        start_utc[:] = self.start_utc

        end_utc = ncout.createVariable('end_utc', np.float32, ('time',))
        end_utc.units = 'unitless'
        end_utc.long_name = 'End UTC hour of coincident TROPOMI and Pandora sampling window'
        end_utc[:] = self.end_utc

        panno2 = ncout.createVariable('panno2', np.float32, ('time',))
        panno2.units = 'molecules/cm2'
        panno2.long_name = 'Pandora error-weighted daily mean total column NO2 coincident with TROPOMI overpass'
        panno2[:] = self.pan_no2
        
        panerr = ncout.createVariable('panerr', np.float32, ('time',))
        panerr.units = 'molecules/cm2'
        panerr.long_name = 'Pandora weighted error of daily mean total columns of NO2 coincident with TROPOMI overpass'
        panerr[:] = self.pan_err
        
        pancnt = ncout.createVariable('pancnt', np.float32, ('time',))
        pancnt.units = 'unitless'
        pancnt.long_name = 'Number of Pandora observations used to obtain weighted mean'
        pancnt[:] = self.pan_cnt
        satno2 = ncout.createVariable('satno2', np.float32, ('time',))
        satno2.units = 'molecules/cm2'
        satno2.long_name = 'S5P/TROPOMI NO2 error-weighted daily mean total column NO2 coincident with Pandora'
        satno2[:] = self.s5p_no2
        satcldh = ncout.createVariable('satcldh', np.float32, ('time',))
        satcldh.units = 'hPa'
        satcldh.long_name = 'S5P/TROPOMI mean cloud top pressure at Pandora site'
        satcldh[:] = self.s5p_ch
        satcldf = ncout.createVariable('satcldf', np.float32, ('time',))
        satcldf.units = 'hPa'
        satcldf.long_name = 'S5P/TROPOMI mean cloud fraction at Pandora site'
        satcldf[:] = self.s5p_cf
        saterr = ncout.createVariable('saterr', np.float32, ('time',))
        saterr.units = 'molecules/cm2'
        saterr.long_name = 'S5P/TROPOMI NO2 weighted error of daily mean total columns of NO2 coincident with the Pandora site'
        saterr[:] = self.s5p_wgt
        satcnt = ncout.createVariable('satcnt', np.float32, ('time',))
        satcnt.units = 'unitless'
        satcnt.long_name = 'Number of S5P/TROPOMI observations used to obtain weighted mean'
        satcnt[:] = self.s5p_cnt
        ncout.close()


class TropomiData:
    """A class for reading, preprocessing and cloud-masking Tropomi data files"""
    def __init__(self, filepath, apply_bias_correction, no2_col):
        """Returns a new instance of CloudComparisonData containing the data from file_path.
        You can also choose whether to apply bias correction and whethere you want the total or troposphere only
        column of this data

        :param filepath: The path to the Tropomi netcdf file
        :type filepath: str
        :param apply_bias_correction: Whether to apply bias correction
        :type apply_bias_correction: bool
        :param no2_col: Whether to use all atmospheric data or just the troposphere
        :type no2_col: str (can be 'Tot' or 'Trop')
        :return: Returns a new CloudComparisonData instance.
        :rtype: TropomiData"""
        # Read file:
        fh = Dataset(filepath, mode='r')
        self.apply_bias_correction = apply_bias_correction
        self.no2_col = no2_col
        # Extract data of interest (lon, lat, clouds, NO2 total column & error):
        glons = fh.groups['PRODUCT'].variables['longitude'][:]
        self.tlons = glons.data[0, :, :]
        glats = fh.groups['PRODUCT'].variables['latitude'][:]
        self.tlats = glats.data[0, :, :]

        # Skip file if no pixels overlap with site:
        difflon = abs(pandora_data.panlon - self.tlons)
        difflat = abs(pandora_data.panlat - self.tlats)
        check_ind=np.where( (difflon<=1) & (difflat<=1) )[0]
        if ( len(check_ind)==0 ):
            raise NoDataException        
        
        self.xdim = len(self.tlats[:, 0])
        self.ydim = len(self.tlats[0, :])
        # Factor to convert from mol/m3 to molecules/cm2:
        self.no2sfac = fh.groups['PRODUCT']. \
            variables['nitrogendioxide_tropospheric' \
                      '_column'].multiplication_factor_to_convert_to_molecules_percm2
        # Get delta-time (along x index):
        gdtime = fh.groups['PRODUCT'].variables['delta_time'][:]
        self.tdtime = gdtime.data[0, :]
        # Get start (reference time):
        greftime = fh.groups['PRODUCT'].variables['time_utc'][:]
        self.treftime = greftime[0, :]
        # Extract UTC hours and minutes:
        gomi_dd = [x[8:10] for x in self.treftime]
        gomi_utc_hh = [x[11:13] for x in self.treftime]
        gomi_min = [x[14:16] for x in self.treftime]
        gomi_utc_hh = [int(i) for i in gomi_utc_hh]
        gomi_min = [int(i) for i in gomi_min]
        gomi_dd = [int(i) for i in gomi_dd]
        # Convert time from 1D to 2D:
        self.tomi_min = np.zeros((self.xdim, self.ydim))
        self.tomi_utc_hh = np.zeros((self.xdim, self.ydim))
        self.tomi_dd = np.zeros((self.xdim, self.ydim))
        for i in range(self.xdim):
            self.tomi_min[i, :] = gomi_min[i]
            self.tomi_utc_hh[i, :] = gomi_utc_hh[i]
            self.tomi_dd[i, :] = gomi_dd[i]
        # Get QA flag scale factor:
        self.qasfac = fh.groups['PRODUCT'].variables['qa_value'].scale_factor
        # QA value:
        self.qaval = fh.groups['PRODUCT'].variables['qa_value'][0, :, :]
        # NO2 fill/missing value:
        self.fillval = fh.groups['PRODUCT'].variables['nitrogendioxide_tropospheric_column']._FillValue
        # Total vertical column NO2 column:
        self.gtotno2 = fh.groups['PRODUCT']['SUPPORT_DATA']['DETAILED_RESULTS'].variables['nitrogendioxide_total_column'][:]
        # Preserve in case use in future:
        # gtotno2=fh.groups['PRODUCT']['SUPPORT_DATA']['DETAILED_RESULTS'].\
        #         variables['nitrogendioxide_summed_total_column'][:]
        self.ttotno2 = self.gtotno2.data[0, :, :]
        # Total slant column:
        gscdno2 = fh.groups['PRODUCT']['SUPPORT_DATA']['DETAILED_RESULTS'].variables[
                      'nitrogendioxide_slant_column_density'][:]
        self.tscdno2 = gscdno2.data[0, :, :]
        # Precision of total slant column:
        gscdno2err = fh.groups['PRODUCT']['SUPPORT_DATA']['DETAILED_RESULTS'] \
                         .variables['nitrogendioxide_slant_column_density_''precision'][:]
        self.tscdno2err = gscdno2err.data[0, :, :]
        # Tropospheric vertical column :
        gtropno2 = fh.groups['PRODUCT'].variables['nitrogendioxide_' \
                                                  'tropospheric_column'][:]
        self.ttropno2 = gtropno2.data[0, :, :]
        # Summed column precision:
        # Preserve in case use in future:
        # ttotno2err=fh.groups['PRODUCT']['SUPPORT_DATA']\
        #            ['DETAILED_RESULTS'].\
        #            variables['nitrogendioxide_summed_total_column_'\
        #                      'precision'][0,:,:]
        # Tropospheric column:
        self.ttropno2err = fh.groups['PRODUCT'].variables['nitrogendioxide_' \
                                                     'tropospheric_column_' \
                                                     'precision'][0, :, :]
        # Total columnn:
        self.ttotno2err = fh.groups['PRODUCT']['SUPPORT_DATA'] \
                         ['DETAILED_RESULTS']. \
                         variables['nitrogendioxide_total_column_precision'] \
            [0, :, :]
        # Statospheric column:
        gstratno2 = fh.groups['PRODUCT']['SUPPORT_DATA']['DETAILED_RESULTS']. \
                        variables['nitrogendioxide_stratospheric_column'][:]
        self.tstratno2 = gstratno2.data[0, :, :]
        # Statospheric column error:
        self.tstratno2err = fh.groups['PRODUCT']['SUPPORT_DATA']['DETAILED_RESULTS']. \
                          variables['nitrogendioxide_stratospheric_column_precision'][0, :, :]
        # Surface pressure:
        gsurfp = fh.groups['PRODUCT']['SUPPORT_DATA']['INPUT_DATA']. \
                     variables['surface_pressure'][:]
        self.tsurfp = gsurfp.data[0, :, :]
        # Solar zenith angle (degrees):
        tsza = fh.groups['PRODUCT']['SUPPORT_DATA']['GEOLOCATIONS']. \
                   variables['solar_zenith_angle'][:]
        self.sza = tsza[0, :, :]
        # Viewing zenith angle (degrees):
        tvza = fh.groups['PRODUCT']['SUPPORT_DATA']['GEOLOCATIONS']. \
                   variables['viewing_zenith_angle'][:]
        self.vza = tvza[0, :, :]
        # Stratospheric AMF:
        gstratamf = fh.groups['PRODUCT']['SUPPORT_DATA']['DETAILED_RESULTS']. \
                        variables['air_mass_factor_stratosphere'][:]
        self.tstratamf = gstratamf.data[0, :, :]
        fh.close()

        # Free memory (LIST NOT YET COMPLETE):
        del tsza
        del tvza
        del glons
        del glats

    def preprocess(self):
        """Prepares the Tropomi data for use. Applies bias correction if needed here.
        Bias correction to stratosphere and troposphere is obtained in this work from comparison of TROPOMI to Pandora over Mauna Loa (stratospheric column) and Izana and Altzomoni (tropospheric column). The correction is confirmed by also comparing TROPOMI and MAX-DOAS tropospheric columns at Izana. 
        """
        # Calculate the geometric AMF:
        self.tamf_geo = np.add((np.reciprocal(np.cos(np.deg2rad(self.sza)))),
                          (np.reciprocal(np.cos(np.deg2rad(self.vza)))))
        # Calculate the total column with a geometric AMF:
        if not self.apply_bias_correction:
            # Step 1: calculate stratospheric SCD (not in data product):
            self.tscdstrat = np.multiply(self.tstratno2, self.tstratamf)
            # Step 2: calculate tropospheric NO2 SCD:
            self.ttropscd = np.subtract(self.tscdno2, self.tscdstrat)
            # Step 3: calculate tropospheric NO2 VCD:
            self.tgeotropvcd = np.divide(self.ttropscd, self.tamf_geo)
            # Step 4: sum up stratospheric and tropospheric NO2 VCDs:
            self.tgeototvcd = np.add(self.tgeotropvcd, self.tstratno2)
            # Calculate total VCD column error by adding in quadrature
            # individual contributions:
            self.ttotvcd_geo_err = np.sqrt(np.add(np.square(self.tstratno2err),
                                                  np.square(self.tscdno2err)))
            # Estimate the tropospheric NO2 error as the total error
            # weighted by the relative contribution of the troposphere
            # to the total column. This can be done as components that
            # contribute to the error are the same:
            self.ttropvcd_geo_err = np.multiply(self.ttotvcd_geo_err,
                                                (np.divide(self.tgeotropvcd, self.tgeototvcd)))

        else:
            # Apply bias correction if indicated in the input arguments:
            # Preserve original stratosphere for error adjustment:
            self.tstratno2_og = self.tstratno2
            # Apply correction to stratosphere based on comparison
            # to Pandora Mauna Loa total columns:
            #self.tstratno2 = np.where(self.tstratno2_og != self.fillval, ( (2.5e15??? / self.no2sfac) + (self.tstratno2_og / 0.79) - (2.8e15??? / self.no2sfac)), np.nan)
            self.tstratno2 = np.where(self.tstratno2_og != self.fillval, ( (self.tstratno2_og / 0.79) - (6.9e14 / self.no2sfac)), np.nan)

            # Step 1: calculate stratospheric SCD (not in data product):
            self.tscdstrat = np.multiply(self.tstratno2, self.tstratamf)
            # Step 2: calculate tropospheric NO2 SCD:
            self.ttropscd = np.subtract(self.tscdno2, self.tscdstrat)
            # Step 3: calculate tropospheric NO2 VCD:
            self.tgeotropvcd = np.divide(self.ttropscd, self.tamf_geo)
            # Apply bias correction to troposphere based on comparison
            # to Pandora and MAX-DOAS Izana tropospheric columns:
            self.tgeotropvcd = self.tgeotropvcd / 1.6

            # The above bias correction has a null effect on the total column,
            # as it just redistributes the relative contribution of the
            # troposphere and the stratosphere.
            # Calculate the correction to the stratospheric column:
            #self.tstratno2 = np.where(self.tstratno2_og != self.fillval, ( (2.5e15??? / self.no2sfac) + (self.tstratno2 / 0.79) - (2.8e15??? / self.no2sfac)), np.nan)
            self.tstratno2 = np.where(self.tstratno2_og != self.fillval, ( (self.tstratno2 / 0.79) - (6.9e14 / self.no2sfac)), np.nan)
            
            # Step 4: sum up stratospheric and tropospheric NO2 VCDs:
            self.tgeototvcd = np.add(self.tgeotropvcd, self.tstratno2)

            # Step 5: calculate updated error estimates for the total,
            # stratospheric and tropospheric columns:
            
            # Calculate the stratospheric column error by scaling the
            # original error by the relative change in the stratospheric
            # column before and after applying correction factors:
            self.tstratno2err = np.where(self.tstratno2err != self.fillval, np.multiply(self.tstratno2err, np.divide(self.tstratno2, self.tstratno2_og)), np.nan)
            # Calculate total column error by adding in quadrature
            # individual contributions:
            self.ttotvcd_geo_err = np.sqrt(np.add(np.square(self.tstratno2err),np.square(self.tscdno2err)))
            # Calculate the tropospheric column error by scaling the original
            # error by the relative change in the tropospheric column after
            # applying correction factors:
            self.ttropvcd_geo_err = np.multiply(self.ttotvcd_geo_err, (np.divide(self.tgeotropvcd, self.tgeototvcd)))

    def apply_cloud_filter(self, cloud_product):
        """Applies a cloud filter and finishes preprocessing.

        :param cloud_product: An instance of CloudData for filtering with
        :type cloud_product: CloudData
        :raises BadCloudShapeException: Raised if  the cloud_product is not the same shape as the Tropomi slice
        """
        # Select which NO2 data to use based on NO2_COL selection:
        if (self.no2_col == 'Tot'):
            self.tno2val = self.tgeototvcd
            self.tno2err = self.ttotvcd_geo_err
        elif (self.no2_col == 'Trop'):
            self.tno2val = self.tgeotropvcd
            self.tno2err = self.ttropvcd_geo_err
            stratcol = self.tstratno2
            totcol = self.tgeototvcd
        else:
            # This should be unreachable, so is undocumented. 
            raise BadNo2ColException

        # Check that data shapes are equal:
        if cloud_product.tcldfrac.shape != self.sza.shape:
            print('Cloud product and NO2 indices ne!', flush=True)
            print(cloud_product.tcldfrac.shape, self.sza.shape, flush=True)
            print('Skipping this swath', flush=True)
            raise BadCloudShapeException

        # Account for files where mask is missing (only appears to be one):
        if len(self.gtotno2.mask.shape) == 0:
            self.tno2val = np.where(self.tno2val == self.fillval, np.nan, self.tno2val)
        else:
            self.tno2val[self.gtotno2.mask[0, :, :]] = float("nan")
        # Find relevant data only:
        # Filter out low quality retrieval scenes (0.45 suggested
        # by Henk Eskes at KNMI):
        self.tno2val = np.where(self.qaval < 0.45, np.nan, self.tno2val)

        # Also set scenes with snow/ice to nan. Not likely for the tropical
        # sites selected for this comparison, but included this here in
        # case of future comparisons that in midlatitudes or poles:
        self.tno2val = np.where(cloud_product.tsnow != 0, np.nan, self.tno2val)
        # Convert NO2 from mol/m3 to molec/cm2:
        self.tno2val = np.multiply(self.tno2val, self.no2sfac)
        self.tno2err = np.multiply(self.tno2err, self.no2sfac)
        # Trim to remove data where relevant NO2 data is not NAN:
        self.lons = self.tlons[~np.isnan(self.tno2val)]
        self.lats = self.tlats[~np.isnan(self.tno2val)]
        self.no2err = self.tno2err[~np.isnan(self.tno2val)]
        self.omi_utc_hh = self.tomi_utc_hh[~np.isnan(self.tno2val)]
        self.omi_min = self.tomi_min[~np.isnan(self.tno2val)]
        self.omi_dd = self.tomi_dd[~np.isnan(self.tno2val)]
        self.cldfrac = cloud_product.tcldfrac[~np.isnan(self.tno2val)]
        self.cldpres = cloud_product.tcldpres[~np.isnan(self.tno2val)]
        self.no2val = self.tno2val[~np.isnan(self.tno2val)]
        if (self.no2_col == 'Trop'):
            self.stratcol = stratcol[~np.isnan(self.tno2val)]
            self.totcol = totcol[~np.isnan(self.tno2val)]
        # Combine hour and minute into xx.xx format:
        self.tomi_hhmm = self.omi_utc_hh + np.divide(self.omi_min, 60.)


class CloudData:
    """A class containing cloud data extracted from either tropomi data or ocra data.
    """
    def __init__(self, filepath, product_type, tropomi_data=None):
        """Returns an instance of the cloud data needed from filtering. This can come from either a freco cloud product
        (part of Tropomi) or a dlr-ocra file

        :param filepath: Path to the file
        :type filepath: str
        :param product_type: Can be 'dlr-ocra' or 'fresco'
        :type product_type: str
        :param tropomi_data: An instance of CloudComparisonData. Required if type is 'fresco'
        :type tropomi_data: TropomiData"""

        if product_type == "o22cld":
            self.get_o22cld_cloud_fields(filepath)
        elif product_type == "fresco":
            self.get_fresco_cloud_fields(filepath, tropomi_data)

    def get_o22cld_cloud_fields(self, filepath):
        """Reads ocra data"""
        # Read data:
        fh = Dataset(filepath, mode='r')
        # Check that date is the same as the gc_data file:
        strdate = filepath[-66:-51]
        # Future improvements to code: Move check elsewhere
        if strdate != tomi_files_on_day[-66:-51]:
            print('NO2 file, Cloud file: ' + strdate + ", " + strdate, flush=True)
            print('EXITING: Files are not for the same date!', flush=True)
            sys.exit()
        # Get cloud fraction and cloud top pressure:
        gcldfrac = fh.groups['PRODUCT'].variables['cloud_fraction'][:]
        self.tcldfrac = gcldfrac.data[0, :, :]
        gcldpres = fh.groups['PRODUCT'].variables['cloud_top_pressure'][:]
        self.tcldpres = np.ma.getdata(gcldpres[0, :, :])  # extract data from masked array
        # QA value:
        self.cldqa = fh.groups['PRODUCT'].variables['qa_value'][0, :, :]
        # Snow/ice flag:
        self.gsnow = fh.groups['PRODUCT']['SUPPORT_DATA']['INPUT_DATA']. \
                    variables['snow_ice_flag'][:]
        self.tsnow = self.gsnow.data[0, :, :]
        # Set poor quality cloud data to nan:
        self.tcldfrac = np.where(self.cldqa < 0.5, np.nan, self.tcldfrac)
        self.tcldpres = np.where(self.cldqa < 0.5, np.nan, self.tcldpres)
        # Set clouds over snow/ice scenes to nan:
        self.tcldfrac = np.where(self.tsnow != 0, np.nan, self.tcldfrac)
        self.tcldpres = np.where(self.tsnow != 0, np.nan, self.tcldpres)

        # Close file:
        fh.close()

    def get_fresco_cloud_fields(self, filepath, tropomi_data):
        """Reads fresco data. Uses tropomi_data to filter for misclassified snow."""
        # FRESCO product is in NO2 file
        fh = Dataset(filepath, mode='r')
        # Cloud input data (cldfrac, cldalb, cldpres):
        gcldfrac = fh.groups['PRODUCT']['SUPPORT_DATA']['INPUT_DATA']. \
                       variables['cloud_fraction_crb'][:]
        self.tcldfrac = gcldfrac.data[0, :, :]
        gcldpres = fh.groups['PRODUCT']['SUPPORT_DATA']['INPUT_DATA']. \
                       variables['cloud_pressure_crb'][:]
        self.tcldpres = np.ma.getdata(gcldpres[0, :, :])  #
        # Snow/ice flag:
        gsnow = fh.groups['PRODUCT']['SUPPORT_DATA']['INPUT_DATA']. \
                    variables['snow_ice_flag'][:]
        # Apparent scene pressure:
        gscenep = fh.groups['PRODUCT']['SUPPORT_DATA']['INPUT_DATA']. \
                      variables['apparent_scene_pressure'][:]
        self.tscenep = gscenep.data[0, :, :]
        self.tsnow = gsnow.data[0, :, :]
        # Convert all valid snow/ice free flag values (252,255) to 0.
        # Ocean values:
        self.tsnow = np.where(self.tsnow == 255, 0, self.tsnow)
        # Coastline values (listed as potential "suspect" in the ATBD
        # document (page 67):
        self.tsnow = np.where(self.tsnow == 252, 0, self.tsnow)
        # Less then 1% snow/ice cover:
        self.tsnow = np.where(self.tsnow < 1, 0, self.tsnow)
        # Snow/ice misclassified as clouds:
        self.tsnow = np.where(((self.tsnow > 80) & (self.tsnow < 104)
                               & (self.tscenep > (0.98 * tropomi_data.tsurfp))),
                                0, self.tsnow)
        # Set clouds over snow/ice scenes to nan:
        self.tcldfrac = np.where(self.tsnow != 0, np.nan, self.tcldfrac)
        self.tcldpres = np.where(self.tsnow != 0, np.nan, self.tcldpres)
        # close file:
        fh.close()


class PandoraData:
    """Extracts and preprocesses pandora data from a pandora datafile. See docs for read_pandora for file details"""
    def __init__(self, file_path, col_type, pan_ver):
        """Returns an instance of PandoraData from file_path. Will apply a correction factor of 0.9 to gc_data and no2_err
        to bring the product up to 'pseudo 1.8'. Also applies corrections for Manua Loa if needed

        :param file_path: Path to the pandora file
        :type file_path: str
        :param col_type: Can be 'Tot' or 'Trop'
        :type col_type: str"""
        # Read Pandora data from external function:
        p = read_pandora(file_path, col_type, pan_ver)
        # Extract latitude and longitude:
        loc = p[0]
        self.panlat = loc['lat']
        self.panlon = loc['lon']
        self.panalt = loc['alt']
        # Extract data frame with relevant Pandora data:
        df = p[1]
        # Get variables names from column headers:
        varnames = df.columns.values
        # Rename Pandora data:
        self.panyy = df.year.values
        self.panmon = df.month.values
        self.pandd = df.day.values
        self.panhh_utc = df.hour_utc.values
        self.panmin = df.minute.values
        # Combine hour and minute into xx.xx format:
        self.pan_hhmm = self.panhh_utc + np.divide(self.panmin, 60.)
        # Change data at the date line (0-2 UTC) to (24-26 UTC) to aid sampling 30
        # minutes around the satellite overpass time at Mauna Loa. This won't
        # affect sampling over Izana, as it's at about 12 UTC.
        sind = np.argwhere((self.pan_hhmm >= 0.) & (self.pan_hhmm < 2.))
        self.pan_hhmm[sind] = self.pan_hhmm[sind] + 24.
        self.panjday = df.jday.values
        self.pansza = df.sza.values
        self.panno2 = df.no2.values
        self.panno2err = df.no2err.values
        self.panqaflag = df.qaflag.values
        self.panfitflag = df.fitflag.values
        # Create pseudo v1.8 data by decreasing Pandora column value and error by 90%.
        # Recommendation by Alexander Cede (email exchange) to account for lower
        # reference temperature at these sites that will be used in the future v1.8
        # retrieval rather than 254K used for sites that extend to the surface.
        # V1.8 data will be available in late 2020.
        # Only apply this correction to the high-altitude sites:
        if (col_type == 'Tot' and self.panalt > 2e3):
            if pan_ver == '1.7':
                print('Apply 10% bias correction to Pandora v1.7 total column at {} m'.format(str(self.panalt)))
                self.panno2 = self.panno2 * 0.9
                self.panno2err = self.panno2err * 0.9
            else:
                print('No 10% bias correction applied to Pandora v1.8 total column at {} m'.format(str(self.panalt)))
        else:
            print('No 10% bias correction applied to Pandora data for site at {} m'.format(str(self.panalt)))
        # Get data length (i.e., length of each row):
        npanpnts = len(df)
        # Confirm processing correct site:
        print('Pandora Site: ', file_path)


def get_tropomi_files_on_day(tropomi_dir, date, no2_prod):
    """Gets a sorted list of tropomi files in tropomi_dir on date

    :param tropomi_dir: The directory containing tropomi files
    :type tropomi_dir: str
    :param date: The date to search for
    :type date: DateTime
    :return: A list of filepaths to tropomi files
    :rtype: list of str
    """
    # Converts the python date object to a set string representation of time
    # In this case, zero-padded year, month and a datestamp of the Sentinel format
    # See https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes
    year = date.strftime(r"%Y")
    month = date.strftime(r"%m")
    datestamp = date.strftime(r"%Y%m%dT")
    tomi_glob_string = os.path.join(tropomi_dir, year, month, 'S5P_' + no2_prod + '_L2__NO2____' + datestamp + '*')
    tomi_files_on_day = glob.glob(tomi_glob_string)
    print('Found {} tropomi files for {}: '.format(len(tomi_files_on_day), date,flush=True))
    tomi_files_on_day = sorted(tomi_files_on_day)
    return tomi_files_on_day


def get_ocra_files_on_day(tropomi_dir, date):
    """Gets a sorted list of tropomi files in tropomi_dir on date

    :param tropomi_dir: The directory containing tropomi files
    :type tropomi_dir: str
    :param date: The date to search for
    :type date: DateTime
    :return: A list of filepaths to ocra files in the tropomi dir
    :rtype: list of str
    """
    # Get string of day:
    year = date.strftime(r"%Y")
    month = date.strftime(r"%m")
    datestamp = date.strftime(r"%Y%m%dT")
    cld_glob_string = os.path.join(tropomi_dir, "CLOUD_OFFL", year, month,
                                   'S5P_OFFL_L2__CLOUD__' + datestamp + '*')
    cldfile = glob.glob(cld_glob_string)[0]
    # Order the files:
    cldfile = sorted(cldfile)
    return cldfile


def get_pandora_file(pan_dir, pandora_site, site_num, c_site, no2_col, fv):
    """Gets the pandora file for the given set of parameters"""
    pandora_glob_string = os.path.join(pan_dir, pandora_site,
                         'Pandora' + site_num + 's1_' + c_site + '_L2' + fv + '.txt')
    return glob.glob(pandora_glob_string)[0]

def get_days_since_data_start(date, data_start = None):
    """Returns the number of days since the start date. If no start date is given, assumed 01/06/2019"""
    if not data_start:
        data_start = dt.datetime(year=2019, month=6, day=1)
    delta = date - data_start
    return delta.days


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--trop_dir")
    parser.add_argument("--pan_dir")
    parser.add_argument("--out_dir")
    parser.add_argument("--no2_col", default="Tot", help="Either Tot or Trop; default is Tot")
    parser.add_argument("--cloud_product", default="fresco", help="options are fresco, dlr-ocra; default is fresco")
    parser.add_argument("--pandora_site", default="izana", help="options are izana,mauna_loa,altzomoni; default is izana")
    parser.add_argument("--str_diff_deg", default="02", help="options are: 03,02,01,005; default is 02")
    parser.add_argument("--str_diff_min", default="30", help="options are: 60,30,15; default is 30")
    parser.add_argument("--apply_bias_correction", type=bool, default=False)
    parser.add_argument("--start_date", default="2019-06-01", help="Start date of processing window (yyyy-mm-dd)")
    parser.add_argument("--end_date", default="2020-05-31", help="End date of processing window (yyyy-mm-dd)")
    parser.add_argument("--no2_prod", default = "PAL_", help="TROPOMI NO2 product name. Can be OFFL or PAL_")
    args = parser.parse_args()

    start_date = dt.datetime.strptime(args.start_date, "%Y-%m-%d")
    end_date = dt.datetime.strptime(args.end_date, "%Y-%m-%d")

    # Set degree range based on string entry.
    if ( args.str_diff_deg== '02'):
        DIFF_DEG=0.2
    if ( args.str_diff_deg== '03'):
        DIFF_DEG=0.3
    if ( args.str_diff_deg== '01'):
        DIFF_DEG=0.1
    if ( args.str_diff_deg== '005'):
        DIFF_DEG=0.05

    # Define time range (in minutes) to sample Pandora around TROPOMI overpass:
    if ( args.str_diff_min=='30' ):
        DIFF_HH=30/60
    if ( args.str_diff_min=='15' ):
        DIFF_HH=15/60
    if ( args.str_diff_min=='60' ):
        DIFF_HH=60/60

    # Get Pandora site number:
    # STILL TO DO: REPLACE FORT MCKAY WITH EGBERT
    if ( args.pandora_site== 'altzomoni'):
        SITE_NUM= '65'
        C_SITE= 'Altzomoni'
    if ( args.pandora_site== 'izana'):
        SITE_NUM= '101'
        C_SITE= 'Izana'
    if ( args.pandora_site== 'mauna_loa_59'):
        SITE_NUM= '59'
        C_SITE= 'MaunaLoaHI'
    if ( args.pandora_site== 'mauna_loa_56'):
        SITE_NUM= '56'
        C_SITE= 'MaunaLoaHI'
    if ( args.pandora_site== 'eureka'):
        SITE_NUM= '144'
        C_SITE= 'Eureka-PEARL'
    if ( args.pandora_site== 'fairbanks'):
        SITE_NUM= '29'
        C_SITE= 'FairbanksAK'
    if ( args.pandora_site== 'fort-mckay'):
        SITE_NUM= '122'
        C_SITE= 'FortMcKay'
    if ( args.pandora_site== 'ny-alesund'):
        SITE_NUM= '152'
        C_SITE= 'NyAlesund'

    # Get Pandora version:
    if ( args.pandora_site== 'altzomoni'):
        pandora_version = '1.8'
    else:
        pandora_version = '1.7'

    # Conditions for choosing total or tropospheric column:
    if ( args.no2_col== 'Trop'):
        if ( C_SITE =='Altzomoni' ):
            FV='_rnvh3p1-8'
        else:
            FV='Trop_rnvh1p1-7'            
        #maxval=3
        Y_MIN=0
        Y_MAX=15
    if ( args.no2_col== 'Tot'):
        #maxval=5
        if ( C_SITE =='Altzomoni' ):
            FV='_rnvs3p1-8'
        else:
            FV= 'Tot_rnvs1p1-7'        
        Y_MIN=10
        Y_MAX=60

    # Get Pandora file_path (one file per site):
    panfile= get_pandora_file(args.pan_dir, args.pandora_site, SITE_NUM, C_SITE, args.no2_col, FV)
    if ( args.apply_bias_correction ):
        outfile = os.path.join(args.out_dir, 'tropomi-pandora-comparison-' + args.pandora_site + '-' + args.cloud_product + '-' + args.no2_col + '-' + args.str_diff_deg + 'deg-' + args.str_diff_min + 'min-bias-corr-v2.nc')
    else:
        outfile = os.path.join(args.out_dir, 'tropomi-pandora-comparison-' + args.pandora_site + '-' + args.cloud_product + '-' + args.no2_col + '-' + args.str_diff_deg + 'deg-' + args.str_diff_min + 'min-v1.nc')

    pandora_data = PandoraData(panfile, args.no2_col, pandora_version)
    data_aggregator = DataCollector(start_date, end_date, pandora_version)

    # In the below code, processing_day is a Python date object
    # They are generated using dateutil's rrule (relative rule) and rdelta(relative delta) functions:
    # https://dateutil.readthedocs.io/en/stable/rrule.html
    # https://dateutil.readthedocs.io/en/stable/relativedelta.html
    # For every day in the time period
    for processing_day in rr.rrule(freq=rr.DAILY, dtstart=start_date, until=end_date):

        print("Processing {}".format(processing_day),flush=True)
        tomi_files_on_day = get_tropomi_files_on_day(args.trop_dir, processing_day, args.no2_prod)

        if args.cloud_product== 'dlr-ocra':
            cloud_files_on_day = get_ocra_files_on_day(args.trop_dir, processing_day)
            # Check for inconsistent number of files:
            if len(cloud_files_on_day) != len(tomi_files_on_day):
                print('NO2 files = ', len(tomi_files_on_day), flush=True)
                print('CLOUD files = ', len(cloud_files_on_day), flush=True)
                print('unequal number of files', flush=True)
                raise UnequalFileException
        elif args.cloud_product == "fresco":
            cloud_files_on_day = tomi_files_on_day
        else:
            raise InvalidCloudProductException

        for tomi_file_on_day, cloud_file_on_day in zip(tomi_files_on_day, cloud_files_on_day):
            try:
                trop_data = TropomiData(tomi_file_on_day, args.apply_bias_correction, args.no2_col)
                trop_data.preprocess()
                cloud_data = CloudData(cloud_file_on_day, args.cloud_product, trop_data)
                trop_data.apply_cloud_filter(cloud_data)
                data_aggregator.set_trop_ind_for_day(processing_day, DIFF_DEG, trop_data, pandora_data)
                data_aggregator.add_trop_data_to_day(processing_day, trop_data)
                for hour in range(data_aggregator.nhrs):
                    data_aggregator.add_pandora_data_to_day(processing_day, hour, DIFF_HH, pandora_data)
            except NoDataException:
                continue
            except NoPandoraException:
                continue

    data_aggregator.apply_weight_to_means()
    data_aggregator.write_to_netcdf(outfile)
    data_aggregator.plot_data(args.pandora_site)
