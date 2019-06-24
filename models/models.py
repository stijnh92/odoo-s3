# -*- coding: utf-8 -*-

#     Odoo-S3
#     Allows you to use Odoo with AWS S3 buckets for file storage.
#     Copyright (C) 2016  Thomas Vanesse
#
#     This program is partly based on a legacy addon written by
#     Hugo Santos <hugo.santos@factolibre.com> in 2014 for Odoo v7.0.
#     The original module and source code can found here:
#       https://www.odoo.com/apps/modules/7.0/document_amazons3/

#     This program is free software: you can redistribute it and/or modify
#     it under the terms of the GNU Affero General Public License as
#     published by the Free Software Foundation, either version 3 of the
#     License, or (at your option) any later version.

#     This program is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU Affero General Public License for more details.

#     You should have received a copy of the GNU Affero General Public License
#     along with this program.  If not, see <http://www.gnu.org/licenses/>.

from odoo import api, models
from odoo.tools import config

import boto
import boto3
import codecs
import base64
import hashlib
import logging
import os

_logger = logging.getLogger(__name__)


class S3Attachment(models.Model):
    """Extends ir.attachment to implement the S3 storage engine
    """
    _inherit = "ir.attachment"

    def _s3_path(self, fname):
        db_name = self._cr.dbname
        return "%s/%s" % (db_name, fname)

    def _parse_storage_url(self, bucket_url):
        # Parse the bucket url
        scheme = bucket_url[:5]
        assert scheme == 's3://', \
            "Expecting an s3:// scheme, got {} instead.".format(scheme)

        try:
            remain = bucket_url.lstrip(scheme)
            access_key_id = remain.split(':')[0]
            remain = remain.lstrip(access_key_id).lstrip(':')
            secret_key = remain.split('@')[0]
            bucket_name = remain.split('@')[1]
        except Exception:
            raise Exception("Unable to parse the S3 bucket url.")

        return access_key_id, secret_key, bucket_name

    def _connect_to_S3_bucket(self, bucket_url):
        try:
            access_key_id, secret_key, bucket_name = self._parse_storage_url(bucket_url)

            if not access_key_id or not secret_key:
                raise Exception(
                    "No AWS access and secret keys were provided."
                    " Unable to establish a connexion to S3."
                )
        except Exception:
            raise Exception("Unable to parse the S3 bucket url.")

        host = config.get('s3_host', 's3.amazonaws.com')
        s3_conn = boto.connect_s3(access_key_id, secret_key, host=host)
        s3_bucket = s3_conn.lookup(bucket_name)
        if not s3_bucket:
            # If the bucket does not exist, create a new one
            s3_bucket = s3_conn.create_bucket(bucket_name)

        return s3_bucket

    def _file_read(self, fname, bin_size=False):
        storage = self._storage()
        if storage[:5] == 's3://':
            s3_path = self._s3_path(fname)
            s3_bucket = self._connect_to_S3_bucket(storage)
            s3_key = s3_bucket.get_key(s3_path)

            if not s3_key:
                # Some old files (prior to the installation of odoo-S3) may
                # still be stored in the file system even though
                # ir_attachment.location is configured to use S3
                try:
                    read = super(S3Attachment, self)._file_read(fname, bin_size=False)
                except Exception:
                    # Could not find the file in the file system either.
                    return False
            else:
                read = base64.b64encode(s3_key.get_contents_as_string())
        else:
            read = super(S3Attachment, self)._file_read(fname, bin_size=False)
        return read

    def _file_write(self, value, checksum):
        storage = self._storage()
        if storage[:5] == 's3://':
            s3_bucket = self._connect_to_S3_bucket(storage)
            bin_value = codecs.decode(value, "base64_codec")
            fname = hashlib.sha1(bin_value).hexdigest()
            s3_path = self._s3_path(fname)

            s3_key = s3_bucket.get_key(s3_path)
            if not s3_key:
                s3_key = s3_bucket.new_key(s3_path)

            s3_key.set_contents_from_string(bin_value)
        else:
            fname = super(S3Attachment, self)._file_write(value, checksum)

        return fname

    @api.model
    def _run_copy_filestore_to_s3(self):
        storage = self._storage()

        if storage[:5] == 's3://':
            db_name = self._cr.dbname
            full_path = self._full_path('')

            access_key_id, secret_key, bucket_name = self._parse_storage_url(storage)

            s3 = boto3.client(
                's3',
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_key,
            )

            for root, dirs, files in os.walk(full_path):
                for file_name in files:
                    path = os.path.join(root, file_name)
                    s3.upload_file(path, bucket_name,  '%s/%s' % (db_name, path[len(full_path):]))
                    _logger.info('S3: Copy %s/%s', db_name, path[len(full_path):])

    @api.model
    def copy_filestore_to_s3(self):
        """This command should be triggered from odoo shell:
        e.g.:
        $> env['ir.attachment'].search([]).copy_filestore_to_s3()
        """
        with api.Environment.manage():
            try:
                self._run_copy_filestore_to_s3()
                _logger.info('S3: filestore copied to S3 successfully')
            except Exception as e:
                _logger.error('S3: filestore copy to S3 aborted: ', e)

    @api.multi
    def check_s3_filestore(self):
        """This command should be triggered from odoo shell:
        e.g.:
        $> res_list, totals = env['ir.attachment'].search([]).check_s3_filestore()
        $> filter(lambda x: x['s3_lost']==True, res_list)
        $> print totals # will show totals
        """
        storage = self._storage()
        if storage[:5] != 's3://':
            return

        s3_bucket = self._connect_to_S3_bucket(storage)

        status_res = []
        totals = {
            'count': 0,
            'lost_count': 0,
        }

        for att in self:
            if att.store_fname:
                status = {}
                status['name'] = att.name
                status['fname'] = att.store_fname
                totals['count'] += 1

                s3_path = self._s3_path(att.store_fname)
                s3_key = s3_bucket.get_key(s3_path)

                if not s3_key:
                    _logger.error('S3: check_s3_filestore was not able to read key:%s from bucket', att.store_fname)
                    status['s3_lost'] = True
                    totals['lost_count'] += 1
                else:
                    _logger.debug('S3: check_s3_filestore read key:%s from bucket successfully', att.store_fname)

                status_res.append(status)

        return status_res, totals
