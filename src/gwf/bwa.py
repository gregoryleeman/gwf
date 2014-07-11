from gwf import *

bwa_index = template(input='{refGenome}.fa', 
                     output=['{refGenome}.amb', '{refGenome}.ann', '{refGenome}.pac']) \
    << 'bwa index -p {refGenome} -a bwtsw {refGenome}.fa'

# FIXME: This doesn't work on slurm where the PBS_JOBID is called SLURM_JOBID. Deal with that!
bwa_map = template(input=['{R1}', '{R2}', '{refGenome}.amb', '{refGenome}.ann', '{refGenome}.pac'],
                   output='{bamfile}', cores=16) << '''

bwa mem -t 16 {refGenome} {R1} {R2} | \
    samtools view -Shb - > /scratch/$PBS_JOBID/unsorted.bam
samtools sort -o /scratch/$PBS_JOBID/unsorted.bam /scratch/$PBS_JOBID/sort | \
    samtools rmdup -s - {bamfile}

'''
